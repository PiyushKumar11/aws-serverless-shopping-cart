import json
import os

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from boto3.dynamodb.conditions import Key

from shared import get_cart_id, get_headers, handle_decimal_type

logger = Logger()
tracer = Tracer()
metrics = Metrics()

dynamodb = boto3.resource("dynamodb")

logger.debug("Initializing DDB Table %s", os.environ["TABLE_NAME"])
table = dynamodb.Table(os.environ["TABLE_NAME"])


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
        logger.info(f"Checkout items in cart for user : {user_id}")
    except KeyError:
        logger.error("checkout_cart: KeyError: Unauthorized token")
        return {
            "statusCode": 400,
            "headers": get_headers(cart_id),
            "body": json.dumps({"message": "Invalid user"}),
        }

    # Get all cart items belonging to the user's identity
    response = table.query(
        KeyConditionExpression=Key("pk").eq(f"user#{user_id}")
        & Key("sk").begins_with("product#"),
        # Perform a strongly consistent read here to ensure we get correct and up to date cart
        ConsistentRead=True,
    )

    cart_items = response.get("Items")
    logger.info(
        f"Fetch existing items in the user#{user_id} cart : {len(cart_items)}")
    # batch_writer will be used to update status for cart entries belonging to the user
    with table.batch_writer() as batch:
        for item in cart_items:
            pk = str(item.get("pk", ""))
            logger.info(f"Checkout the item in cart : {pk}")
            # Delete ordered items
            batch.delete_item(Key={"pk": pk, "sk": item["sk"]})
            logger.info(f"Remove the checked out item from cart : {pk}")

    metrics.add_metric(name="CartCheckedOut", unit="Count", value=1)
    logger.info({"action": "CartCheckedOut", "cartItems": cart_items})
    logger.info(
        f"Successfully checked out all items from the user#{user_id} cart")

    return {
        "statusCode": 200,
        "headers": get_headers(cart_id),
        "body": json.dumps(
            {"products": response.get("Items")}, default=handle_decimal_type
        ),
    }
