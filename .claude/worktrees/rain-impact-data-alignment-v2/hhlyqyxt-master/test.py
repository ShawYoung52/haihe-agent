import json
from utils import build_rainstorm_impact_map_from_url

view = build_rainstorm_impact_map_from_url(
    "http://10.226.107.130:4396/rainstorm_impact_output/rainstorm_impact_202607021500_202607031500_2fea323f/rainstorm_impact_map.json"
)

print(json.dumps(view, ensure_ascii=False, indent=2))