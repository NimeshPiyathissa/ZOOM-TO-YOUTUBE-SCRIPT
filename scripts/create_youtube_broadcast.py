#!/usr/bin/env python3
"""
OPTIONAL helper: create an Unlisted YouTube live broadcast + stream via the
YouTube Data API v3, and bind them together. Prints the RTMP ingestion URL
(to put in .env as part of the target, though YT_STREAM_KEY is what
stream.sh actually uses), and the watch URL.

You do NOT need this script - creating the broadcast by hand in YouTube
Studio (see README) is simpler for one-off streams. Use this only if you
want to script/repeat the setup.

Setup:
  1. In Google Cloud Console, enable the "YouTube Data API v3" for a project.
  2. Create an OAuth 2.0 Client ID (type: Desktop app), download it as
     client_secret.json next to this script.
  3. pip install --user google-api-python-client google-auth-oauthlib
  4. python3 create_youtube_broadcast.py "My Stream Title"

First run opens a browser for OAuth consent (run this on your own machine,
not headless on the VPS, unless you set up a device-flow / copy-paste flow).
A token.json is cached next to this script for future runs.
"""

import sys
import json
import pathlib

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/youtube"]
HERE = pathlib.Path(__file__).parent
CLIENT_SECRET_FILE = HERE / "client_secret.json"
TOKEN_FILE = HERE / "token.json"


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET_FILE.exists():
                sys.exit(f"Missing {CLIENT_SECRET_FILE} - see the docstring for setup steps.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def main():
    if len(sys.argv) < 2:
        sys.exit(f"Usage: {sys.argv[0]} \"Stream Title\"")
    title = sys.argv[1]

    youtube = build("youtube", "v3", credentials=get_credentials())

    broadcast = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails",
        body={
            "snippet": {
                "title": title,
                "scheduledStartTime": "2030-01-01T00:00:00Z",  # placeholder; go live manually
            },
            "status": {
                "privacyStatus": "unlisted",
                "selfDeclaredMadeForKids": False,
            },
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": True,
                "latencyPreference": "normal",
            },
        },
    ).execute()

    stream = youtube.liveStreams().insert(
        part="snippet,cdn,contentDetails",
        body={
            "snippet": {"title": f"{title} - stream"},
            "cdn": {
                "frameRate": "30fps",
                "ingestionType": "rtmp",
                "resolution": "1080p",
            },
            "contentDetails": {"isReusable": True},
        },
    ).execute()

    youtube.liveBroadcasts().bind(
        id=broadcast["id"], part="id,contentDetails", streamId=stream["id"]
    ).execute()

    ingestion = stream["cdn"]["ingestionInfo"]
    print(json.dumps({
        "broadcast_id": broadcast["id"],
        "watch_url": f"https://youtube.com/watch?v={broadcast['id']}",
        "studio_url": f"https://studio.youtube.com/video/{broadcast['id']}/livestreaming",
        "rtmp_ingestion_address": ingestion["ingestionAddress"],
        "stream_key": ingestion["streamName"],
    }, indent=2))
    print("\nPut stream_key above into YT_STREAM_KEY in your .env.")


if __name__ == "__main__":
    main()
