#!/usr/bin/env bash
#
# Walks the PII auto-tagging demo end to end. Each step pauses so you can show the UI
# between them; pass --no-pause to run it straight through.
#
#   ai-orchestrator/scripts/demo.sh [dataset_name]
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GMS="${DATAHUB_GMS_URL:-http://localhost:8080}"
ORCH="${AI_ORCHESTRATOR_URL:-http://localhost:8000}"
TABLE="${1:-crm.demo_live}"
URN="urn:li:dataset:(urn:li:dataPlatform:mysql,${TABLE},PROD)"
TOKEN="$(grep '^DATAHUB_GMS_TOKEN=' "$REPO/ai-orchestrator/.env" | cut -d= -f2- | tr -d '"')"

PAUSE=1
if [[ "${2:-}" == "--no-pause" || "${1:-}" == "--no-pause" ]]; then
    PAUSE=0
fi

step() {
    echo
    echo "──────────────────────────────────────────────────────────────"
    echo "  $1"
    echo "──────────────────────────────────────────────────────────────"
    if [[ $PAUSE -eq 1 ]]; then
        read -rp "  [enter to run] " _
    fi
}

tags_now() {
    curl -s -H "Authorization: Bearer $TOKEN" \
        "$GMS/openapi/v3/entity/dataset/$URN/editableSchemaMetadata" |
        python3 -c "
import json, sys
body = json.load(sys.stdin)
fields = (body.get('value') or {}).get('editableSchemaFieldInfo') or []
if not fields:
    print('  (no tags)')
for f in fields:
    names = [t['tag'].rsplit(':', 1)[-1] for t in (f.get('globalTags') or {}).get('tags') or []]
    print(f\"  {f['fieldPath']:<18} {names}\")
"
}

queue_now() {
    curl -s "$ORCH/api/pii/pending" | python3 -c "
import json, sys
body = json.load(sys.stdin)
print(f\"  badge count: {body['total']}\")
for ds in body['datasets']:
    print(f\"  {ds['dataset']}\")
    for v in ds['verdicts']:
        print(f\"    {v['field']:<16} {v['label']:<18} {round(v['confidence'] * 100)}%  {v['reason'][:60]}\")
"
}

step "1. Nothing exists yet"
echo "  dataset: HTTP $(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
    "$GMS/openapi/v3/entity/dataset/$URN/schemaMetadata")"
queue_now

step "2. Ingest a schema — a plain GMS write, nothing PII-aware about it"
python3 - "$TABLE" <<'PY' > /tmp/demo_payload.json
import json, sys
def col(name, native, kind="StringType"):
    return {"fieldPath": name,
            "type": {"type": {f"com.linkedin.schema.{kind}": {}}},
            "nativeDataType": native}
print(json.dumps({"value": {
    "schemaName": sys.argv[1],
    "platform": "urn:li:dataPlatform:mysql",
    "version": 0, "hash": "",
    "platformSchema": {"com.linkedin.schema.MySqlDDL": {"tableSchema": ""}},
    "fields": [
        col("id", "BIGINT", "NumberType"),
        col("email_address", "VARCHAR(255)"),
        col("mobile_number", "VARCHAR(32)"),
        col("full_name", "VARCHAR(128)"),
        col("home_postcode", "VARCHAR(16)"),
        # deliberately ambiguous — these are what land in the review queue
        col("contact", "VARCHAR(255)"),
        col("handle", "VARCHAR(64)"),
        col("order_total", "DECIMAL(12,2)", "NumberType"),
    ],
}}))
PY
curl -s -o /dev/null -w '  POST schemaMetadata: HTTP %{http_code}\n' -X POST \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    --data @/tmp/demo_payload.json \
    "$GMS/openapi/v3/entity/dataset/$URN/schemaMetadata?createIfNotExists=false"

echo "  waiting for the action to pick up the change event..."
sleep 22
docker logs "$(docker ps -qf name=datahub-actions-debug)" 2>&1 | grep "$TABLE" | tail -1 | sed 's/^/  /'

step "3. Tags are on the dataset — nobody asked for them"
tags_now

step "4. The uncertain ones went to review instead of being written"
queue_now
echo
echo "  ▸ Open the assistant at http://localhost:3000 — the floating button carries a badge,"
echo "    and the Review tab lists these with Apply / Reject per row."

step "5. Undo everything the AI wrote (human tags are left alone)"
curl -s -X POST "$ORCH/api/pii/revert" -H 'Content-Type: application/json' \
    -d "{\"dataset_urn\":\"$URN\",\"resolved_by\":\"demo\"}" |
    python3 -c "
import json, sys
body = json.load(sys.stdin)
print(f\"  reverted {body['reverted']} verdict(s) across {len(body['columns_cleared'])} column(s)\")
"
tags_now

echo
echo "Ledger for this dataset:"
docker exec "$(docker ps -qf name=mysql)" mysql -N -B -udatahub -pdatahub datahub -e \
    "SELECT field_path, label, ROUND(confidence,2), tier, status, COALESCE(resolved_by,'-')
       FROM pii_verdicts WHERE dataset_urn='$URN' ORDER BY tier, field_path;" 2>/dev/null |
    sed 's/^/  /'
