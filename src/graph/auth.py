"""Microsoft identity platform v2 OAuth refresh-token exchange.

We never persist access tokens — they're short-lived. The refresh token in
Secrets Manager is rotated on each call (MSA refresh tokens rotate; AAD
refresh tokens stay valid). The caller is responsible for writing a new
refresh token back to Secrets Manager when one is returned.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import boto3
import httpx

logger = logging.getLogger(__name__)

GRAPH_SCOPES = (
    "https://graph.microsoft.com/Mail.Read "
    "https://graph.microsoft.com/Calendars.ReadWrite "
    "https://graph.microsoft.com/Tasks.ReadWrite "
    "https://graph.microsoft.com/Files.ReadWrite "
    "https://graph.microsoft.com/User.Read "
    "offline_access"
)


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_in: int


async def exchange_refresh_token(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> TokenBundle:
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": GRAPH_SCOPES,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()

    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", refresh_token),
        expires_in=int(body.get("expires_in", 3600)),
    )


def rotate_refresh_token_secret(secret_id: str, new_refresh_token: str) -> None:
    """Write a rotated refresh token back to Secrets Manager (preserving the
    other fields in the JSON blob)."""
    client = boto3.client("secretsmanager")
    current = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    if current.get("refresh_token") == new_refresh_token:
        return
    current["refresh_token"] = new_refresh_token
    client.put_secret_value(SecretId=secret_id, SecretString=json.dumps(current))
    logger.info("Rotated refresh token in %s", secret_id)
