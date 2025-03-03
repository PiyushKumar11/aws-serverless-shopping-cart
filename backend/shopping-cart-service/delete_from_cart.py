import json
import os

import boto3
from aws_lambda_powertools import Logger, Tracer

logger = Logger()
tracer = Tracer()

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
def lambda_handler(event, context):
    """
    Handle messages from SQS Queue containing cart items, and delete them from DynamoDB.
    """
    try:
        records = event["Records"]
    except KeyError:
        logger.error("delete_from_total: KeyError: No records found")
    logger.info(f"Deleting {len(records)} records")
    with table.batch_writer() as batch:
        for item in records:
            pk = item_body.get("pk", "")
            logger.info(f"Deleting item - {pk} from cart")
            item_body = json.loads(item["body"])
            batch.delete_item(
                Key={"pk": item_body["pk"], "sk": item_body["sk"]})

    return {
        "statusCode": 200,
    }
