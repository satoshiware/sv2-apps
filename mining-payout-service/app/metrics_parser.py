import re
from typing import Dict

# Example lines:
# sv2_server_shares_accepted_total{channel_id="1",user_identity="baveet.miner1"} 42
# sv1_client_shares_accepted_total{client_id="1",user_identity="baveet.miner1"} 42
PATTERN = re.compile(
    r'^sv(?:1_client|2_server)_shares_accepted_total\{[^}]*user_identity="(?P<identity>[^"]+)"[^}]*\}\s+(?P<value>\d+(?:\.\d+)?)$'
)


def parse_accepted_shares(metrics_text: str) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for line in metrics_text.splitlines():
        m = PATTERN.match(line.strip())
        if not m:
            continue
        values[m.group("identity")] = int(float(m.group("value")))
    return values
