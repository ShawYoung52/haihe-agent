from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, List

import requests


def _auth(user: str, pwd: str):
    return (user, pwd)


def _build_colormap_sld(style_name: str, ramp: str = "RdYlGn") -> str:
    # 按 ramp 选择色带（降水 mm 分级）。避免依赖外部 geoserver 包。
    rp = (ramp or "").strip().lower()
    cmap_type = "ramp"
    if rp in {"raindiscrete", "rain_discrete", "discrete"}:
        # 离散分级，更接近业务专题图观感
        cmap_type = "intervals"
        entries = [
            (0, "#ffffff", 0.00),
            # 低雨量降低存在感，减少南部浅蓝噪点观感
            (0.1, "#d9f0ff", 0.12),
            (10, "#9fd9ff", 0.38),
            (25, "#73c884", 0.92),
            (50, "#3aa657", 1.00),
            (100, "#f0df4f", 1.00),
            (150, "#f5a544", 1.00),
            (250, "#ea6b3a", 1.00),
            (350, "#cf3a3a", 1.00),
            (500, "#7f2d7a", 1.00),
        ]
    elif rp in {"rainpretty", "rain_pretty", "pretty"}:
        # 视觉更友好：低值更淡，高值更醒目（蓝-绿-黄-橙-红）
        entries = [
            (0, "#ffffff", 0.00),
            (0.1, "#eaf6ff", 0.40),
            (5, "#bfe5ff", 0.75),
            (10, "#7dc8ff", 0.85),
            (25, "#56b870", 0.90),
            (50, "#9fd65b", 0.95),
            (100, "#f4df6d", 0.98),
            (150, "#f5b35e", 1.00),
            (250, "#ef7b45", 1.00),
            (350, "#d7443f", 1.00),
            (500, "#8d2b7a", 1.00),
        ]
    else:
        entries = [
            (0, "#f7f7f7", 1.00),
            (0.1, "#ffffcc", 1.00),
            (10, "#c2e699", 1.00),
            (25, "#78c679", 1.00),
            (50, "#31a354", 1.00),
            (100, "#fee08b", 1.00),
            (150, "#fdae61", 1.00),
            (250, "#f46d43", 1.00),
            (350, "#d73027", 1.00),
        ]
    cm = "\n".join(
        f'            <ColorMapEntry color="{c}" quantity="{q}" opacity="{o}" />'
        for q, c, o in entries
    )
    title = f"{style_name}_{ramp}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld"
  xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <NamedLayer>
    <Name>{title}</Name>
    <UserStyle>
      <Title>{title}</Title>
      <FeatureTypeStyle>
        <Rule>
          <RasterSymbolizer>
            <Opacity>1.0</Opacity>
            <ColorMap type="{cmap_type}">
{cm}
            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""


def upsert_style_and_bind(
    base_url: str,
    workspace: str,
    layer_name: str,
    style_name: str,
    user: str,
    pwd: str,
    *,
    ramp: str = "RdYlGn",
) -> None:
    base = base_url.rstrip("/")
    sld = _build_colormap_sld(style_name=style_name, ramp=ramp)
    style_url = f"{base}/rest/workspaces/{workspace}/styles/{style_name}.sld"
    create_url = f"{base}/rest/workspaces/{workspace}/styles?name={style_name}"
    h = {"Content-Type": "application/vnd.ogc.sld+xml"}

    # 先尝试创建，存在时回退更新
    cr = requests.post(create_url, auth=_auth(user, pwd), data=sld.encode("utf-8"), headers=h, timeout=30)
    if cr.status_code not in (200, 201):
        ur = requests.put(style_url, auth=_auth(user, pwd), data=sld.encode("utf-8"), headers=h, timeout=30)
        if ur.status_code not in (200, 201):
            raise RuntimeError(f"样式创建/更新失败 {style_name}: create={cr.status_code}, update={ur.status_code}")

    bind_url = f"{base}/rest/layers/{workspace}:{layer_name}.json"
    bind_payload = {"layer": {"defaultStyle": {"name": style_name, "workspace": workspace}}}
    br = requests.put(bind_url, auth=_auth(user, pwd), json=bind_payload, timeout=30)
    if br.status_code not in (200, 201):
        raise RuntimeError(f"绑定样式失败 {workspace}:{layer_name} -> {style_name}: {br.status_code}")


def ensure_workspace(base_url: str, workspace: str, user: str, pwd: str) -> None:
    u = f"{base_url}/rest/workspaces/{workspace}.json"
    r = requests.get(u, auth=_auth(user, pwd), timeout=30)
    if r.status_code == 200:
        return
    if r.status_code != 404:
        raise RuntimeError(f"查询 workspace 失败: {r.status_code} {r.text[:200]}")
    cu = f"{base_url}/rest/workspaces"
    payload = {"workspace": {"name": workspace}}
    cr = requests.post(cu, auth=_auth(user, pwd), json=payload, timeout=30)
    if cr.status_code not in (200, 201):
        raise RuntimeError(f"创建 workspace 失败: {cr.status_code} {cr.text[:200]}")


def publish_geotiff(base_url: str, workspace: str, layer_name: str, tif_path: str, user: str, pwd: str) -> Dict[str, str]:
    base = base_url.rstrip("/")
    store = layer_name

    url = (
        f"{base}/rest/workspaces/{workspace}/coveragestores/{store}/file.geotiff"
        f"?configure=all&coverageName={layer_name}"
    )
    with open(tif_path, "rb") as f:
        r = requests.put(
            url,
            auth=_auth(user, pwd),
            data=f,
            headers={"Content-Type": "image/tiff"},
            timeout=120,
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"发布 GeoTIFF 失败 {layer_name}: {r.status_code} {r.text[:300]}")

    wms = (
        f"{base}/wms?service=WMS&version=1.1.0&request=GetMap&layers={workspace}:{layer_name}"
        f"&styles=&bbox={{minx}},{{miny}},{{maxx}},{{maxy}}&width={{width}}&height={{height}}"
        f"&srs=EPSG:4326&format=image/png&transparent=true"
    )
    return {"layer": f"{workspace}:{layer_name}", "tif": tif_path, "wms_template": wms}


def _layer_name(kind: str, hours: int, suffix: str = "") -> str:
    k = (kind or "").strip().lower()
    if k in ("obs", "observation"):
        base = f"obs_apcp_{hours}h"
        return f"{base}_{suffix}" if suffix else base
    if k in ("fcst", "forecast", "ec"):
        base = f"fcst_apcp_{hours}h"
        return f"{base}_{suffix}" if suffix else base
    raise ValueError(f"unknown kind: {kind!r}")


def _extract_tail_timestamp(path_or_name: str) -> str:
    stem = Path(path_or_name).stem
    # 1) 优先兼容尾部时间戳：xxx_20260506170149.tif
    m = re.search(r"_(20\d{8,14})$", stem)
    if m:
        return str(m.group(1))
    # 2) 兼容预报命名：ec_2025072900_rain_total_12h.tif
    m = re.search(r"^ec_(20\d{8,14})_rain_total_\d{1,3}h$", stem, flags=re.IGNORECASE)
    if m:
        return str(m.group(1))
    # 3) 兜底：提取最后一个连续时间串
    all_ts = re.findall(r"(20\d{8,14})", stem)
    return str(all_ts[-1]) if all_ts else ""


def infer_named_tifs(tif_dir: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in tif_dir.glob("*.tif*"):
        name = p.name.lower()
        # 实况：haihe_obs_idw_12h.tif / haihe_obs_idw_24h.tif ...
        m_obs = re.search(r"(haihe_)?obs(_idw)?_(\d{1,3})h", name)
        if m_obs:
            h = int(m_obs.group(3))
            out[f"obs{h}"] = str(p)
            continue

        # 预报：ec_2023073000_rain_total_12h.tif / ..._72h.tif
        m_ec = re.search(r"ec_.*rain_total_(\d{1,3})h", name)
        if m_ec:
            h = int(m_ec.group(1))
            out[f"fcst{h}"] = str(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="批量发布累计降水 GeoTIFF 到 GeoServer")
    ap.add_argument("--base-url", default=os.getenv("GEOSERVER_BASE_URL", "http://127.0.0.1:8090/geoserver"))
    ap.add_argument("--username", default=os.getenv("GEOSERVER_USER", "admin"))
    ap.add_argument("--password", default=os.getenv("GEOSERVER_PASSWORD", "geoserver"))
    ap.add_argument("--workspace", default=os.getenv("GEOSERVER_WORKSPACE", "nee"))
    ap.add_argument(
        "--tif-dir",
        required=True,
        help="tif 目录，支持识别：haihe_obs_idw_XXh.tif（实况）与 ec_*_rain_total_XXh.tif（预报）",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--style-ramp", default="RdYlGn", help="色带名称标签（当前内置降水色带）")
    ap.add_argument("--no-style", action="store_true", help="仅发布图层，不生成/绑定色带样式")
    ap.add_argument(
        "--append-layer-timestamp",
        action="store_true",
        help="图层名追加 tif 文件尾部时间戳（如 *_20260506170149）",
    )
    args = ap.parse_args()

    tif_dir = Path(args.tif_dir)
    if not tif_dir.exists():
        raise SystemExit(f"目录不存在: {tif_dir}")

    named = infer_named_tifs(tif_dir)
    if not named:
        raise SystemExit(f"未识别到 tif，请检查目录与命名: {tif_dir}")

    # 当要求追加时间后缀时，若个别文件名本身不带时间（如 obs_12h.tif），
    # 则回退使用同批次其它文件可识别出的时间戳（通常来自 fcst 文件）。
    batch_ts = ""
    if args.append_layer_timestamp:
        for _k, _p in named.items():
            t = _extract_tail_timestamp(_p)
            if t:
                batch_ts = t
                break

    if args.dry_run:
        print(json.dumps({"base_url": args.base_url, "workspace": args.workspace, "resolved": named}, ensure_ascii=False, indent=2))
        return

    ensure_workspace(args.base_url, args.workspace, args.username, args.password)

    published: List[Dict[str, str]] = []
    # key: obs12 / fcst72 ...
    def _sort_key(k: str) -> tuple:
        kind_rank = 0 if k.startswith("obs") else 1
        try:
            hh = int(re.sub(r"\D+", "", k))
        except Exception:
            hh = 999
        return (kind_rank, hh, k)

    for key in sorted(named.keys(), key=_sort_key):
        tif_path = named[key]
        kind = "obs" if key.startswith("obs") else "fcst"
        hours = int(re.sub(r"\D+", "", key))
        ts_suffix = ""
        if args.append_layer_timestamp:
            ts_suffix = _extract_tail_timestamp(tif_path) or batch_ts
        layer = _layer_name(kind, hours, suffix=ts_suffix)
        item = publish_geotiff(
            base_url=args.base_url,
            workspace=args.workspace,
            layer_name=layer,
            tif_path=tif_path,
            user=args.username,
            pwd=args.password,
        )
        published.append(item)
        if not args.no_style:
            style_name = layer
            upsert_style_and_bind(
                base_url=args.base_url,
                workspace=args.workspace,
                layer_name=layer,
                style_name=style_name,
                user=args.username,
                pwd=args.password,
                ramp=args.style_ramp,
            )
        print(f"[ok] {item['layer']} <- {item['tif']}")

    print(json.dumps({"published": published}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
