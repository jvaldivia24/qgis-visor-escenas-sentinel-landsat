from qgis.core import (
    QgsGeometry, QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject
)


def geom_from_xy(x: float, y: float, epsg_authid: str):
    crs = QgsCoordinateReferenceSystem(epsg_authid)
    pt = QgsPointXY(float(x), float(y))
    geom = QgsGeometry.fromPointXY(pt)
    return geom, crs


def _buffer_meters_epsg4326(geom: QgsGeometry, crs: QgsCoordinateReferenceSystem, meters: float) -> QgsGeometry:
    """Crea un buffer (en metros) alrededor del centroid en WGS84.

    Nota: usa EPSG:3857 como CRS métrico (aprox.) para distancias de pocos km.
    """
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    metric = QgsCoordinateReferenceSystem("EPSG:3857")

    g_wgs = QgsGeometry(geom)

    if crs.authid() != "EPSG:4326":
        tr_to_wgs = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
        g_wgs.transform(tr_to_wgs)

    tr_to_m = QgsCoordinateTransform(wgs84, metric, QgsProject.instance())
    tr_to_wgs = QgsCoordinateTransform(metric, wgs84, QgsProject.instance())

    g_m = QgsGeometry(g_wgs)
    g_m.transform(tr_to_m)

    c = g_m.centroid()
    buf_m = c.buffer(float(meters), 48)

    buf_wgs = QgsGeometry(buf_m)
    buf_wgs.transform(tr_to_wgs)
    return buf_wgs


def buffer_5km_epsg4326(geom: QgsGeometry, crs: QgsCoordinateReferenceSystem) -> QgsGeometry:
    return _buffer_meters_epsg4326(geom, crs, 5000.0)


def buffer_3km_epsg4326(geom: QgsGeometry, crs: QgsCoordinateReferenceSystem) -> QgsGeometry:
    return _buffer_meters_epsg4326(geom, crs, 3000.0)
