import pickle

import networkx as nx


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


if __name__ == '__main__':

    print(analysisRiverByName('南运河'))