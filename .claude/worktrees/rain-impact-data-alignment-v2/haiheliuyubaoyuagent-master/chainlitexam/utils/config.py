import os

# MUSIC (天擎) API 配置
MUSIC_CONFIG = {
    "service_ip": os.getenv("MUSIC_SERVICE_IP", "10.226.90.120"),
    "service_node_id": os.getenv("MUSIC_SERVICE_NODE_ID", "NMIC_MUSIC_CMADAAS"),
    "user_id": os.getenv("MUSIC_USER_ID", "BETJ_QXT_LYGXPT"),
    "password": os.getenv("MUSIC_PASSWORD", "Qxtly@2022ww"),
    "timeout": int(os.getenv("MUSIC_TIMEOUT", "120")),
}

# 数据库配置
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "10.226.107.130"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),
}