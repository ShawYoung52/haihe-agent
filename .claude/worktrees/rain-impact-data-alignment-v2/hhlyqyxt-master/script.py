import pandas
import geopandas

if __name__ == '__main__':

    df = pandas.read_json("test.json")
    gdf = geopandas.GeoDataFrame(
        df, geometry=geopandas.points_from_xy(df['Lon'], df['Lat']))

    print(gdf.head())

    gdf.to_file(
        "test.shp" )