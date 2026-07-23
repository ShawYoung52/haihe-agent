import pickle
import configparser
from pathlib import Path


def main():
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")
    path = Path(config.get("paths", "graph"))

    with path.open("rb") as f:
        G = pickle.load(f)

    # 打印前 10 条边的属性，帮助确认字段名
    for i, (u, v, attr) in enumerate(G.edges(data=True)):
        print(f"{i}: {u} -> {v} | attrs = {attr}")
        if i >= 9:
            break


if __name__ == "__main__":
    main()

