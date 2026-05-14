import os
import json
import logging
import boto3
from supabase import create_client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ECS_CLUSTER = os.environ["ECS_CLUSTER"]
ECS_TASK_DEFINITION = os.environ["ECS_TASK_DEFINITION"]
SUBNETS = os.environ["SUBNETS"].split(",")
SECURITY_GROUPS = os.environ["SECURITY_GROUPS"].split(",")
CONTAINER_NAME = os.environ["CONTAINER_NAME"]

ecs_client = boto3.client("ecs")

def fetch_regions():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    response = supabase.table("regions").select("region").execute()
    return [row["region"] for row in response.data]


def dispatch_task(region):
    task_response = ecs_client.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEFINITION,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": SUBNETS,
                "securityGroups": SECURITY_GROUPS,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "command": ["sh", "-c", "python main.py \"$REGION\""],
                    "environment": [
                        {"name": "REGION", "value": region},
                    ],
                }
            ]
        },
    )

    if task_response["failures"]:
        reason = task_response["failures"][0]["reason"]
        raise RuntimeError(reason)

    return [t["taskArn"] for t in task_response["tasks"]]


def lambda_handler(event, context):
    regions = fetch_regions()
    logger.info(f"Fetched {len(regions)} regions from supported_regions")

    results = []
    failures = []

    for region in regions:
        try:
            task_arns = dispatch_task(region)
            results.append({"region": region, "taskArns": task_arns})
            logger.info(f"Dispatched task for {region}: {task_arns}")
        except Exception as error:
            logger.error(f"Failed to dispatch task for {region}: {error}")
            failures.append({"region": region, "error": str(error)})

    status_code = 200 if not failures else 207
    return {
        "statusCode": status_code,
        "body": json.dumps({
            "total_regions": len(regions),
            "dispatched": len(results),
            "failed": len(failures),
            "results": results,
            "failures": failures,
        }),
    }
