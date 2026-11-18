"""Real-pipe metadata-only MCP fixture; never a qualified native server."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from mcp_live_catalog import RawCatalogPeer

parser = argparse.ArgumentParser()
parser.add_argument("--era", choices=("modern", "legacy"), default="modern")
parser.add_argument("--variant", default="normal")
parser.add_argument("--linger-after-eof", action="store_true")
args = parser.parse_args()
peer = RawCatalogPeer(
    era=args.era, variant="normal" if args.variant == "notification" else args.variant
)

for line in sys.stdin:
    request = json.loads(line)
    if args.variant == "notification" and request.get("method") == "prompts/list":
        print(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True
        )
    reply = asyncio.run(peer.exchange(request, "id" in request))
    if reply is not None:
        print(json.dumps(reply), flush=True)

if args.linger_after_eof:
    time.sleep(30)
