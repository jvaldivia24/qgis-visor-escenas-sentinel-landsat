import numpy as np
from osgeo import gdal
from qgis.core import QgsTask, QgsMessageLog, Qgis


class PercentileStretchTask(QgsTask):
    """
    Calcula percentiles 2-98 para un raster multibanda (3 bandas) en background.
    """
    def __init__(self, description: str, raster_path: str, p_low=2, p_high=98):
        super().__init__(description, QgsTask.CanCancel)
        self.raster_path = raster_path
        self.p_low = p_low
        self.p_high = p_high
        self.result = None
        self.error = None

    def _band_percentiles(self, band_index: int):
        ds = gdal.Open(self.raster_path)
        if ds is None:
            raise RuntimeError(f"No se pudo abrir raster: {self.raster_path}")
        band = ds.GetRasterBand(band_index)
        arr = band.ReadAsArray()
        if arr is None:
            raise RuntimeError("No se pudo leer banda.")
        nodata = band.GetNoDataValue()
        arr = arr.astype(np.float32)
        if nodata is not None:
            arr = arr[arr != nodata]
        arr = arr[np.isfinite(arr)]
        if arr.size < 100:
            mn = float(np.nanmin(arr)) if arr.size else 0.0
            mx = float(np.nanmax(arr)) if arr.size else 1.0
            if mx <= mn:
                mx = mn + 1.0
            return mn, mx
        vmin = float(np.percentile(arr, self.p_low))
        vmax = float(np.percentile(arr, self.p_high))
        if vmax <= vmin:
            vmin = float(np.min(arr))
            vmax = float(np.max(arr))
            if vmax <= vmin:
                vmax = vmin + 1.0
        return vmin, vmax

    def run(self):
        try:
            r = self._band_percentiles(1)
            if self.isCanceled():
                return False
            g = self._band_percentiles(2)
            if self.isCanceled():
                return False
            b = self._band_percentiles(3)
            if self.isCanceled():
                return False
            self.result = {"r": r, "g": g, "b": b}
            return True
        except Exception as e:
            self.error = str(e)
            QgsMessageLog.logMessage(self.error, "SceneBrowser", Qgis.Critical)
            return False
