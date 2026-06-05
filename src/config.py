"""Runtime configuration.

Non-secret values come from environment variables, secrets from AWS Secrets
Manager. Local dry runs fall back to env vars entirely.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

import boto3


@dataclass
class Config:
    # Secrets
    graph_client_id: str
    graph_client_secret: str
    graph_tenant_id: str
    graph_refresh_token: str
    anthropic_api_key: str  # empty when running through Bedrock

    # Knobs
    anthropic_model: str = "claude-opus-4-7"
    aws_region: str = "us-east-1"  # used for Bedrock; ignored if model isn't prefixed
    agenda_sender: str = ""
    agenda_recipient: str = ""
    state_bucket: str = ""
    lookback_days: int = 7
    max_messages: int = 1000  # broad mailbox scan needs more headroom than Inbox-only
    max_sent_messages: int = 150
    max_attachment_bytes: int = 10 * 1024 * 1024  # 10 MB
    todo_list_name: str = "EMIS"
    onedrive_folder: str = "EMIS"
    create_calendar_event: bool = True
    create_todo_tasks: bool = True
    upload_to_onedrive: bool = True
    web_ui_token: str = ""        # required for web UI access
    web_ui_url: str = ""          # base URL of the Web UI Lambda; embeds in email headers
    dry_run: bool = False


def _load_secret(secret_id: str) -> dict:
    raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    return json.loads(raw)


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _is_bedrock_model(model: str) -> bool:
    return model.startswith(("anthropic.", "us.anthropic.", "eu.anthropic.", "apac.anthropic."))


@lru_cache(maxsize=1)
def load_config() -> Config:
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
    aws_region = os.getenv("AWS_REGION", "us-east-1")
    using_bedrock = _is_bedrock_model(model)

    if os.getenv("DRY_RUN") == "1" and os.getenv("GRAPH_REFRESH_TOKEN"):
        # When on Bedrock, ANTHROPIC_API_KEY is optional — AWS creds from the
        # default boto3 chain are used instead.
        anthropic_api_key = os.environ["ANTHROPIC_API_KEY"] if not using_bedrock else os.getenv("ANTHROPIC_API_KEY", "")
        return Config(
            graph_client_id=os.environ["GRAPH_CLIENT_ID"],
            graph_client_secret=os.getenv("GRAPH_CLIENT_SECRET", ""),
            graph_tenant_id=os.getenv("GRAPH_TENANT_ID", "common"),
            graph_refresh_token=os.environ["GRAPH_REFRESH_TOKEN"],
            anthropic_api_key=anthropic_api_key,
            anthropic_model=model,
            aws_region=aws_region,
            agenda_sender=os.getenv("AGENDA_SENDER", ""),
            agenda_recipient=os.getenv("AGENDA_RECIPIENT", ""),
            state_bucket=os.getenv("STATE_BUCKET", ""),
            todo_list_name=os.getenv("TODO_LIST_NAME", "EMIS"),
            onedrive_folder=os.getenv("ONEDRIVE_FOLDER", "EMIS"),
            create_calendar_event=_bool("CREATE_CALENDAR_EVENT", True),
            create_todo_tasks=_bool("CREATE_TODO_TASKS", True),
            upload_to_onedrive=_bool("UPLOAD_TO_ONEDRIVE", True),
            web_ui_token=os.getenv("WEB_UI_TOKEN", ""),
            web_ui_url=os.getenv("WEB_UI_URL", ""),
            dry_run=True,
        )

    graph = _load_secret(os.environ["GRAPH_SECRET_ID"])
    # Skip the Anthropic secret entirely on Bedrock — AWS auth comes from the
    # Lambda execution role.
    anthropic_api_key = "" if using_bedrock else _load_secret(os.environ["ANTHROPIC_SECRET_ID"])["api_key"]
    return Config(
        graph_client_id=graph["client_id"],
        graph_client_secret=graph.get("client_secret", ""),
        graph_tenant_id=graph.get("tenant_id", "common"),
        graph_refresh_token=graph["refresh_token"],
        anthropic_api_key=anthropic_api_key,
        anthropic_model=model,
        aws_region=aws_region,
        agenda_sender=os.environ["AGENDA_SENDER"],
        agenda_recipient=os.environ["AGENDA_RECIPIENT"],
        state_bucket=os.environ["STATE_BUCKET"],
        todo_list_name=os.getenv("TODO_LIST_NAME", "EMIS"),
        onedrive_folder=os.getenv("ONEDRIVE_FOLDER", "EMIS"),
        create_calendar_event=_bool("CREATE_CALENDAR_EVENT", True),
        create_todo_tasks=_bool("CREATE_TODO_TASKS", True),
        upload_to_onedrive=_bool("UPLOAD_TO_ONEDRIVE", True),
        web_ui_token=os.getenv("WEB_UI_TOKEN", ""),
        web_ui_url=os.getenv("WEB_UI_URL", ""),
        dry_run=False,
    )
