import json
import os
import threading

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from boto3.dynamodb.conditions import Key

from shared import generate_ttl, get_cart_id, get_headers, handle_decimal_type

logger = Logger()
tracer = Tracer()
metrics = Metrics()

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])
sqs = boto3.resource("sqs")
queue = sqs.Queue(os.environ["DELETE_FROM_CART_SQS_QUEUE"])


@tracer.capture_method
def update_item(user_id, item):
    """
    Update an item in the database, adding the quantity of the passed in item to the quantity of any products already
    existing in the cart.
    """
    logger.info(f"Updating item quantity in the cart for user#{user_id}")
    logger.info(f"Quantity updated to : ", item.get("quantity", 0))
    ttl = generate_ttl(days=30)
    logger.info(f"Item's time to live in cart : ", ttl)
    table.update_item(
        Key={"pk": f"user#{user_id}", "sk": item["sk"]},
        ExpressionAttributeNames={
            "#quantity": "quantity",
            "#expirationTime": "expirationTime",
            "#productDetail": "productDetail",
        },
        ExpressionAttributeValues={
            ":val": item["quantity"],
            ":ttl": ttl,
            ":productDetail": item["productDetail"],
        },
        UpdateExpression="ADD #quantity :val SET #expirationTime = :ttl, #productDetail = :productDetail",
    )
    logger.info(f"Successfully updated quantity for item#{item['sk']}")


@metrics.log_metrics(capture_cold_start_metric=True)
@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
def lambda_handler(event, context):
    """
    Update cart table to use user identifier instead of anonymous cookie value as a key. This will be called when a user
    is logged in.
    """

    cart_id, _ = get_cart_id(event["headers"])
    try:
        # Because this method is authorized at API gateway layer, we don't need to validate the JWT claims here
        user_id = event["requestContext"]["authorizer"]["claims"]["sub"]
        logger.info(
            "LoggedIn session detected. Migrating cart_id %s to user_id %s", cart_id, user_id)
    except KeyError:
        logger.error("migrate_cart: KeyError: Unauthorized token")
        return {
            "statusCode": 400,
            "headers": get_headers(cart_id),
            "body": json.dumps({"message": "Invalid user"}),
        }

    # Get all cart items belonging to the user's anonymous identity
    logger.info(
        f"Get all cart items belonging to the user's anonymous identity - cart#{cart_id}")
    response = table.query(
        KeyConditionExpression=Key("pk").eq(f"cart#{cart_id}")
        & Key("sk").begins_with("product#")
    )
    unauth_cart = response["Items"]

    # Since there's no batch operation available for updating items, and there's no dependency between them, we can
    # run them in parallel threads.
    thread_list = []

    for item in unauth_cart:
        # Store items with user identifier as pk instead of "unauthenticated" cart ID
        # Using threading library to perform updates in parallel
        logger.info(f"Store items with user#{user_id} - {item}")
        ddb_updateitem_thread = threading.Thread(
            target=update_item, args=(user_id, item)
        )
        thread_list.append(ddb_updateitem_thread)
        ddb_updateitem_thread.start()

        # Delete items with unauthenticated cart ID
        # Rather than deleting directly, push to SQS queue to handle asynchronously
        logger.info(f"Pushing item message to SQS")
        queue.send_message(MessageBody=json.dumps(
            item, default=handle_decimal_type))

    for ddb_thread in thread_list:
        ddb_thread.join()  # Block main thread until all updates finished

    if unauth_cart:
        metrics.add_metric(name="CartMigrated", unit="Count", value=1)

    response = table.query(
        KeyConditionExpression=Key("pk").eq(f"user#{user_id}")
        & Key("sk").begins_with("product#"),
        ProjectionExpression="sk,quantity,productDetail",
        # Perform a strongly consistent read here to ensure we get correct values after updates
        ConsistentRead=True,
    )

    product_list = response.get("Items", [])
    logger.info(
        f"Migrate ${len(product_list)} from anonymous session - cart#{cart_id} to authenticated session - user#{user_id}")
    for product in product_list:
        product.update(
            (k, v.replace("product#", "")) for k, v in product.items() if k == "sk"
        )

    logger.info(
        f"Items is cart successfully migrated from anonymous session - cart#{cart_id} to authenticated session - user#{user_id}")
    return {
        "statusCode": 200,
        "headers": get_headers(cart_id),
        "body": json.dumps({"products": product_list}, default=handle_decimal_type),
    }
