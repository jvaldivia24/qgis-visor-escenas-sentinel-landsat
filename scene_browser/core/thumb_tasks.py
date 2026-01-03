import os
import uuid
import numpy as np
from osgeo import gdal
from qgis.core import QgsTask, QgsMessageLog, Qgis

def _src_from_href(href: str) -> str:
    """Return GDAL VSI path for href; supports s3:// requester-pays."""
    if not href:
        return "/vsicurl/"
    href = str(href)
    if href.startswith('s3://'):
        gdal.SetConfigOption('AWS_NO_SIGN_REQUEST', 'YES')
        gdal.SetConfigOption('AWS_REQUEST_PAYER', 'requester')
        return '/vsis3/' + href[len('s3://'):]
    if href.startswith('http://') or href.startswith('https://'):
        return '/vsicurl/' + href
    return href



def _band_minmax(ds, band_index: int):
    b = ds.GetRasterBand(band_index)
    # Try fast stats; fall back to min/max
    try:
        stats = b.GetStatistics(True, True)  # approxOK=True, force=True
        if stats and stats[0] is not None and stats[1] is not None:
            mn, mx = float(stats[0]), float(stats[1])
            if mn != mx and not (mn is None or mx is None):
                return mn, mx
    except Exception:
        pass

    try:
        mn, mx = b.ComputeRasterMinMax(True)
        mn, mx = float(mn), float(mx)
        if mn != mx:
            return mn, mx
    except Exception:
        pass

    # Fallback: safe defaults (avoid crash)
    return 0.0, 1.0


class ThumbnailTask(QgsTask):
    """Genera un thumbnail PNG real desde 3 assets COG (R,G,B) en un bbox (EPSG:4326).

    Compatible con GDAL de QGIS (sin usar TranslateOptions(scale=...)).
    - Recorta AOI con projWin (EPSG:4326).
    - Crea VRT (separate=True) y exporta PNG en Byte usando scaleParams por banda.
    """

    def __init__(self, description: str, cache_dir: str, assets_dict: dict, rgb_assets: list, bbox4326, size_px=512):
        super().__init__(description, QgsTask.CanCancel)
        self.cache_dir = cache_dir
        self.assets_dict = assets_dict
        self.rgb_assets = rgb_assets
        self.bbox = bbox4326  # (xmin, ymin, xmax, ymax)
        self.size_px = int(size_px)
        self.png_path = None
        self.error = None

    def run(self):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            uid = uuid.uuid4().hex
            # Modo índice: rgb_assets = ["__NDVI__", nir, red] o ["__NBR__", nir, swir2]
            if self.rgb_assets and isinstance(self.rgb_assets[0], str) and self.rgb_assets[0].startswith("__"):
                mode = self.rgb_assets[0].strip("_").upper()
                a1 = self.rgb_assets[1]
                a2 = self.rgb_assets[2]
                if mode == "NBR" and a2 not in self.assets_dict and "swir2" in self.assets_dict:
                    a2 = "swir2"

                tifs = []
                for a in [a1, a2]:
                    href = self.assets_dict[a]["href"]
                    src = _src_from_href(href)
                    out_tif = os.path.join(self.cache_dir, f"thumb_{uid}_{a}.tif")
                    gdal.Translate(
                        out_tif,
                        src,
                        options=gdal.TranslateOptions(
                            projWin=[self.bbox[0], self.bbox[3], self.bbox[2], self.bbox[1]],
                            projWinSRS="EPSG:4326",
                            format="GTiff",
                            width=self.size_px,
                            height=self.size_px,
                            creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
                        ),
                    )
                    tifs.append(out_tif)

                ds1 = gdal.Open(tifs[0]); ds2 = gdal.Open(tifs[1])
                b1 = ds1.GetRasterBand(1).ReadAsArray().astype("float32")
                b2 = ds2.GetRasterBand(1).ReadAsArray().astype("float32")
                denom = (b1 + b2)
                with np.errstate(divide='ignore', invalid='ignore'):
                    idx = (b1 - b2) / denom
                idx[~np.isfinite(idx)] = -9999.0

                out_idx = os.path.join(self.cache_dir, f"thumb_{uid}_{mode}.tif")
                drv = gdal.GetDriverByName("GTiff")
                out_ds = drv.Create(out_idx, ds1.RasterXSize, ds1.RasterYSize, 1, gdal.GDT_Float32, options=["TILED=YES", "COMPRESS=DEFLATE"])
                out_ds.SetGeoTransform(ds1.GetGeoTransform())
                out_ds.SetProjection(ds1.GetProjection())
                ob = out_ds.GetRasterBand(1)
                ob.WriteArray(idx)
                ob.SetNoDataValue(-9999.0)
                ob.FlushCache()
                out_ds.FlushCache()
                out_ds = None

                vrt = os.path.join(self.cache_dir, f"thumb_{uid}_{mode}.vrt")
                gdal.BuildVRT(vrt, [out_idx, out_idx, out_idx], separate=True)

                scale = [-1.0, 1.0, 0.0, 255.0]
                png_path = os.path.join(self.cache_dir, f"thumb_{uid}.png")
                gdal.Translate(
                    png_path,
                    vrt,
                    options=gdal.TranslateOptions(
                        format="PNG",
                        outputType=gdal.GDT_Byte,
                        scaleParams=[scale, scale, scale],
                        noData=-9999.0,
                    ),
                )
                self.png_path = png_path
                return True

            tifs = []

            xmin, ymin, xmax, ymax = self.bbox
            ulx, uly, lrx, lry = xmin, ymax, xmax, ymin

            for a in self.rgb_assets:
                href = self.assets_dict[a]["href"]
                src = _src_from_href(href)
                out_tif = os.path.join(self.cache_dir, f"thumb_{uid}_{a}.tif")

                gdal.Translate(
                    out_tif,
                    src,
                    options=gdal.TranslateOptions(
                        projWin=[ulx, uly, lrx, lry],
                        projWinSRS="EPSG:4326",
                        format="GTiff",
                        creationOptions=["TILED=YES", "COMPRESS=DEFLATE"]
                    )
                )
                tifs.append(out_tif)

                if self.isCanceled():
                    return False

            vrt = os.path.join(self.cache_dir, f"thumb_{uid}.vrt")
            gdal.BuildVRT(vrt, tifs, separate=True)

            ds = gdal.Open(vrt, gdal.GA_ReadOnly)
            if ds is None:
                raise RuntimeError("No se pudo abrir el VRT para calcular min/max.")

            mn1, mx1 = _band_minmax(ds, 1)
            mn2, mx2 = _band_minmax(ds, 2)
            mn3, mx3 = _band_minmax(ds, 3)

            # Avoid zero-range
            def _fix(mn, mx):
                if mn == mx:
                    return mn, mn + 1.0
                return mn, mx

            mn1, mx1 = _fix(mn1, mx1)
            mn2, mx2 = _fix(mn2, mx2)
            mn3, mx3 = _fix(mn3, mx3)

            png = os.path.join(self.cache_dir, f"thumb_{uid}.png")

            gdal.Translate(
                png,
                vrt,
                options=gdal.TranslateOptions(
                    format="PNG",
                    width=self.size_px,
                    height=self.size_px,
                    outputType=gdal.GDT_Byte,
                    bandList=[1, 2, 3],
                    scaleParams=[
                        [mn1, mx1, 0, 255],
                        [mn2, mx2, 0, 255],
                        [mn3, mx3, 0, 255],
                    ],
                )
            )

            self.png_path = png
            QgsMessageLog.logMessage(f"Thumbnail listo: {png}", "SceneBrowser", Qgis.Info)
            return True

        except Exception as e:
            self.error = str(e)
            QgsMessageLog.logMessage(f"Thumbnail error: {self.error}", "SceneBrowser", Qgis.Warning)
            return False