import cartopy.feature as cfeature
from cartopy.io import shapereader
def drawShp(ax, shpurl, zorder=0, linewidth=0.6, edgecolor='k', facecolor='none'):
    world_vector_map = cfeature.ShapelyFeature(shapereader.Reader(shpurl).geometries(), ax.projection, edgecolor=edgecolor,
                                            facecolor=facecolor)
    ax.add_feature(world_vector_map, linewidth=linewidth, zorder=zorder)
    return