import pickle

import networkx as nx
from fastmcp import FastMCP


mcp = FastMCP("weather-tools")

@mcp.tool()
def get_weather(city: str) -> str:
    """查询城市天气"""
    print(city)
    return f"{city} 当前晴天，气温 6℃"

@mcp.tool()
def get_xialiu_rivername(river: str) -> str:
    """根据提供的河流名称查询下流河有哪些"""
    riverlist = analysisRiverByName(river)
    riverlist = list(riverlist)
    return ",".join(riverlist)




def analysisRiverByName(rivername):
    # G = nx.read_adjlist(r"E:\temp\graph.adjlist")

    with open(r"E:\temp\graph.pkl", "rb") as f:
        G = pickle.load(f)



    river_edges = [end for start,end, attr in G.edges(data=True) if attr.get('rivername') == rivername]

    res =[]
    for end in river_edges:

        res.extend(get_downstream_edges(G,end))

    river_name=[]
    for item in res:
        river_name.append(item[2]['rivername'])

    return set(river_name)


def get_downstream_edges(G, node):
    sub_nodes = {node} | nx.descendants(G, node)
    subgraph = G.subgraph(sub_nodes)
    return list(subgraph.edges(data=True))

if __name__ == "__main__":
    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=3333
    )