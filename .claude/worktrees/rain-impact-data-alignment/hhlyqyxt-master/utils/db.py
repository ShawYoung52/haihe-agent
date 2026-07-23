from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# todo 做成配置
# engine = create_engine("postgresql+psycopg2://postgres:postgres@211.157.132.19:48091/hhly")
engine = create_engine("postgresql+psycopg2://postgres:postgres@10.226.107.130:5432/postgres")

Session = sessionmaker(bind=engine)
