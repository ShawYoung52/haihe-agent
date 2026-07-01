# SQL 驱动矢量 WMS 渲染服务

这个服务按请求里的 `sql_id` 到 SQL 注册表读取 `sql_text`，再执行该 SQL 获取矢量几何，最后按请求样式渲染为透明 PNG。支持标准 WMS `GetMap` 的 `bbox/width/height` 请求，也支持常用 XYZ tile 请求。

## 启动

```powershell
$env:WMS_DATABASE_URL="postgresql+psycopg2://user:password@host:5432/dbname"
$env:WMS_SQL_TABLE="wms_sql_registry"
$env:WMS_SQL_ID_COLUMN="id"
$env:WMS_SQL_TEXT_COLUMN="sql_text"
python -m wms_vector_service
```

如果数据库驱动没有安装，需要安装你的 SQLAlchemy URL 对应驱动，例如 PostgreSQL 常用 `psycopg2-binary`。渲染依赖 GeoPandas/Shapely/Matplotlib，部署环境需要先安装项目依赖。

## SQL 注册表

```sql
create table wms_sql_registry (
  id text primary key,
  sql_text text not null
);
```

SQL 推荐返回 GeoJSON 字段，服务会自动识别 `geom_geojson`、`geometry_geojson`、`geojson`、`geom_wkb`、`geometry_wkb`、`wkb`、`geom`、`geometry`。

PostGIS 示例：

```sql
insert into wms_sql_registry(id, sql_text) values (
  'river',
  $$
  select ST_AsGeoJSON(ST_Transform(geom, :srid)) as geom_geojson
  from public.river
  where ST_Intersects(
    ST_Transform(geom, :srid),
    ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)
  )
  $$
);
```

服务会向存储 SQL 传入 `:minx/:miny/:maxx/:maxy/:bbox_minx/:bbox_miny/:bbox_maxx/:bbox_maxy/:srid` 参数。tile 接口默认使用 EPSG:4326，可通过 `crs=EPSG:3857` 切换到 Web Mercator。

## 请求示例

XYZ tile：

```text
http://localhost:8008/tiles/10/843/388.png?sql_id=river&stroke=%230066cc&width=2
```

WMS GetMap：

```text
http://localhost:8008/wms?service=WMS&request=GetMap&sql_id=river&bbox=116,39,117,40&width=256&height=256&crs=EPSG:4326&format=image/png&stroke=%230066cc&fill=%2366ccff&fill_opacity=0.25
```

也可以传 JSON 样式：

```text
style={"stroke":"#0066cc","fill":"#66ccff","width":2,"radius":5,"fillOpacity":0.3}
```

## 几何判断

服务根据数据库返回的几何类型自动判断点、线、面：

- `Point/MultiPoint`：圆点，使用 `fill/stroke/radius/width`
- `LineString/MultiLineString`：线，使用 `stroke/width`
- `Polygon/MultiPolygon`：面，使用 `fill/stroke/width`

当前实现使用 GeoPandas/Shapely/Matplotlib 渲染数据库查询出的几何，复杂投影转换建议放在 SQL 中用 `ST_Transform` 完成。默认坐标系是 EPSG:4326，如需恢复 3857，可设置 `WMS_DEFAULT_SRID=3857` 或在 tile 请求里加 `crs=EPSG:3857`。
