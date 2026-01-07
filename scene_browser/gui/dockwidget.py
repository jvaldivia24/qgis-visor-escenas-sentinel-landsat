import requests
import os
import uuid
import numpy as np
from functools import partial

def _src_from_href(href: str) -> str:
    """Return a GDAL VSI path for a STAC asset href.

    EarthSearch Landsat assets may be s3:// (requester pays). We enable unsigned
    requester-pays access and use /vsis3/. For http(s) we use /vsicurl/.
    """
    if not href:
        return "/vsicurl/"  # will raise with clearer error downstream
    href = str(href)
    if href.startswith("s3://"):
        # Public requester-pays bucket; unsigned access
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
        gdal.SetConfigOption("AWS_REQUEST_PAYER", "requester")
        return "/vsis3/" + href[len("s3://"):]
    if href.startswith("http://") or href.startswith("https://"):
        return "/vsicurl/" + href
    return href


from qgis.PyQt.QtCore import QDate, QSize, Qt
from qgis.PyQt.QtGui import QIcon, QPixmap, QImage, QPainter, QPen, QBrush
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QLineEdit, QDateEdit,
    QScrollArea, QGridLayout, QMessageBox, QSpinBox,
    QToolButton,
    QSlider
)

from qgis.core import (
    QgsRectangle,
    QgsPointXY,
    QgsGeometry,
    QgsRasterLayer, QgsProject, QgsApplication,
    QgsMultiBandColorRenderer, QgsContrastEnhancement,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform
)

from osgeo import gdal

from ..core.aoi import geom_from_xy, buffer_5km_epsg4326, buffer_3km_epsg4326
from ..core.stac_client import StacClient
from ..core.render_tasks import PercentileStretchTask
from ..core.thumb_tasks import ThumbnailTask


EARTH_SEARCH_STAC = "https://earth-search.aws.element84.com/v1"
PLANETARY_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"


# Landsat Collection 2 Level-2 Surface Reflectance scaling (matches GEE)
LS_SR_MULT = 0.0000275
LS_SR_ADD  = -0.2


def stac_datetime_range(date_ini_qdate: QDate, date_fin_qdate: QDate) -> str:
    s = date_ini_qdate.toString("yyyy-MM-dd")
    e = date_fin_qdate.toString("yyyy-MM-dd")
    return f"{s}T00:00:00Z/{e}T23:59:59Z"


def s2_assets_for_preset(preset: str):
    if preset == "Natural":
        return ["red", "green", "blue"]
    if preset == "Infrarrojo":
        return ["nir", "red", "green"]
    if preset == "Agricultura":
        return ["swir16", "nir", "blue"]
    if preset == "NDVI":
        return ["__NDVI__", "nir", "red"]
    if preset == "NBR":
        return ["__NBR__", "nir", "swir22"]
    return ["red", "green", "blue"]


def s2_rgb_for_thumbnail():
    return ["red", "green", "blue"]


def ls_assets_for_preset(preset: str, available_assets):
    def pick(name):
        return name if name in available_assets else None

    if preset == "Natural":
        return [pick("red"), pick("green"), pick("blue")]
    if preset == "Infrarrojo":
        return [pick("nir08") or pick("nir"), pick("red"), pick("green")]
    if preset == "Agricultura":
        return [pick("swir16") or pick("swir1"), pick("nir08") or pick("nir"), pick("blue")]
    if preset == "NDVI":
        return ["__NDVI__", (pick("nir08") or pick("nir")), pick("red")]
    if preset == "NBR":
        return ["__NBR__", (pick("nir08") or pick("nir")), (pick("swir22") or pick("swir2"))]
    return [pick("red"), pick("green"), pick("blue")]


def ls_rgb_for_thumbnail(available_assets):
    rgb = [a for a in ["red", "green", "blue"] if a in available_assets]
    return rgb if len(rgb) == 3 else None


class SceneBrowserDock(QDockWidget):
    def __init__(self, iface):
        super().__init__("Visor de escenas Sentinel 2 y Landsat -Sernanp")
        self.iface = iface

        self.stac_s2 = StacClient(EARTH_SEARCH_STAC)
        self.stac_ls = StacClient(PLANETARY_STAC)  # usar Element84 para evitar signing/409

        self._last_buffer = None  # QgsGeometry EPSG:4326 (5 km, para preview)
        self._last_thumb_buffer = None  # QgsGeometry EPSG:4326 (3 km, para thumbnails)

        self._thumb_tasks = {}  # keep refs to tasks to avoid GC
        self._last_feats = []
        self._last_thumb_kind = None
        self._pc_token_cache = {}  # collection_id -> token


        # card visuals
        self._thumb_icon_px = 240   # bigger thumbnail in card
        self._thumb_png_px = 512    # generated png resolution
        self._thumb_zoom_level = 5
        self._scene_buttons = []  # refs para actualizar tamaño
        self._build_ui()

    def _set_status_info(self, text):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: #1565c0;")  # azul

    def _set_status_ok(self, text):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: #2e7d32; font-weight: bold;")  # verde

    def _set_status_error(self, text):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: #c62828; font-weight: bold;")  # rojo

    def _build_ui(self):
        root = QWidget()
        main = QHBoxLayout(root)

        # LEFT
        left = QVBoxLayout()

        # HEADER_SERNANP_UI
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 8)

        logo_lbl = QLabel()
        logo_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "resources", "sernanp_logo.png"))
        pm = QPixmap(logo_path)
        if not pm.isNull():
            logo_lbl.setPixmap(pm)
        logo_lbl.setFixedHeight(28)
        logo_lbl.setScaledContents(True)

        title_lbl = QLabel("Subdirección de Informacion Estratégica e Investigación de las ANP/DDE")
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("font-size: 8px; font-weight: bold;")

        header.addWidget(logo_lbl, 0)
        header.addSpacing(2)
        header.addWidget(title_lbl, 1)
        
        left.addLayout(header)

        left.setSpacing(3)

        self.cmb_sensor = QComboBox()
        self.cmb_sensor.addItems([
            "Sentinel-2",
            "Landsat 5-9",
        ])

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItems(["Natural", "Infrarrojo", "Agricultura"])

        self.txt_x = QLineEdit("-74.56")
        self.txt_y = QLineEdit(" -9.08")
        self.txt_x.setPlaceholderText("X (lon) - ej: -74.56")
        self.txt_y.setPlaceholderText("Y (lat) - ej: -9.08")

        self.date_ini = QDateEdit()
        self.date_fin = QDateEdit()
        self.date_ini.setCalendarPopup(True)
        self.date_fin.setCalendarPopup(True)
        self.date_ini.setDate(QDate.currentDate().addDays(-60))
        self.date_fin.setDate(QDate.currentDate())

        self.spin_cloud = QSpinBox()
        self.spin_cloud.setRange(0, 100)
        self.spin_cloud.setValue(20)

        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(1, 200)
        self.spin_limit.setValue(5)

        self.btn_search = QToolButton()
        self.btn_search.setText("Buscar escenas")
        self.btn_search.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_search.setMinimumHeight(32)

        self.btn_ndvi = QToolButton()
        self.btn_ndvi.setText("NDVI de escena seleccionada")
        self.btn_ndvi.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_ndvi.setMinimumHeight(32)
        self.btn_ndvi.setEnabled(False)

        self.btn_nbr = QToolButton()
        self.btn_nbr.setText("NBR de escena seleccionada")
        self.btn_nbr.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_nbr.setMinimumHeight(32)
        self.btn_nbr.setEnabled(False)

        self._last_selected_feature = None
        self._last_selected_kind = None  # "s2" o "ls"

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#444;")

        left.addWidget(QLabel("Sensor"))
        left.addWidget(self.cmb_sensor)
        left.addWidget(QLabel("Combinación de bandas"))
        left.addWidget(self.cmb_preset)
        left.addWidget(QLabel("Coordenadas (EPSG:4326)"))
        left.addWidget(self.txt_x)
        left.addWidget(self.txt_y)
        left.addWidget(QLabel("Rango de fechas"))
        left.addWidget(self.date_ini)
        left.addWidget(self.date_fin)
        left.addWidget(QLabel("Nubosidad máxima (%)"))
        left.addWidget(self.spin_cloud)
        left.addWidget(QLabel("Máx. escenas mostradas"))
        left.addWidget(self.spin_limit)
        left.addWidget(QLabel("Zoom a la escena"))
        self.slider_zoom = QSlider(Qt.Horizontal)
        self.slider_zoom.setRange(1, 10)
        self.slider_zoom.setValue(5)  # tamaño medio
        self.lbl_zoom = QLabel("")
        self._thumb_zoom_level = self.slider_zoom.value()
        self._apply_thumb_zoom()
        left.addWidget(self.slider_zoom)
        left.addWidget(self.lbl_zoom)
        left.addWidget(self.btn_search)
        left.addWidget(self.btn_ndvi)
        left.addWidget(self.btn_nbr)
        self.slider_zoom.valueChanged.connect(self.on_thumb_zoom_changed)
        left.addWidget(self.lbl_status)
        left.addStretch()

        # RIGHT
        right = QVBoxLayout()
        right.setSpacing(6)
        lbl_scenes = QLabel("Escenas (más reciente → más antigua)")
        lbl_scenes.setAlignment(Qt.AlignCenter)
        right.addWidget(lbl_scenes)


        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)

        self.grid_container = QWidget()
        self.grid = QGridLayout(self.grid_container)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)

        self.scroll.setWidget(self.grid_container)
        right.addWidget(self.scroll)

        main.addLayout(left, 1)
        main.addLayout(right, 2)
        self.setWidget(root)

        self.btn_search.clicked.connect(self.search_scenes)
        self.btn_ndvi.clicked.connect(self.on_ndvi_clicked)
        self.btn_nbr.clicked.connect(self.on_nbr_clicked)
        self.cmb_preset.currentIndexChanged.connect(self.refresh_scene_list)
    def _thumb_icon_size_from_level(self, level: int) -> int:
        """level 1..10 -> icon px 160..420"""
        level = max(1, min(10, int(level)))
        return int(160 + (level - 1) * (420 - 160) / 9)

    def _apply_thumb_zoom(self):
        px = self._thumb_icon_size_from_level(getattr(self, "_thumb_zoom_level", 5))
        self._thumb_icon_px = px
        for btn in getattr(self, "_scene_buttons", []):
            try:
                btn.setIconSize(QSize(px, px))
                btn.setMinimumHeight(px + 90)
            except Exception:
                pass
        try:
            self.lbl_zoom.setText("")
        except Exception:
            pass

    def on_thumb_zoom_changed(self, val: int):
        self._thumb_zoom_level = int(val)
        self._apply_thumb_zoom()

    def _compute_index_for_last(self, mode: str):
        if not self._last_selected_feature or not self._last_selected_kind:
            QMessageBox.information(self, "Scene Browser", "Primero seleccione una escena (clic en su thumbnail / cargar al visor).")
            return

        feature = self._last_selected_feature
        kind = self._last_selected_kind
        assets = feature.get("assets", {}) or {}
        if not assets:
            QMessageBox.warning(self, "Scene Browser", "La escena seleccionada no tiene assets disponibles.")
            return

        scene_id = feature.get("id") or feature.get("scene_id") or "scene"
        layer_title = f"{mode.upper()} | {scene_id}"

        try:
            if kind == "s2":
                if mode.upper() == "NDVI":
                    a1, a2 = "nir", "red"
                else:
                    a1 = "nir"
                    a2 = "swir22" if "swir22" in assets else ("swir2" if "swir2" in assets else None)
                    if a2 is None:
                        raise RuntimeError("No se encontró banda SWIR2 (swir22/swir2).")
                rlayer, vrt, buf = self._crop_assets_to_index_tif(assets, mode.upper(), a1, a2, "s2", layer_title)
            else:
                nir = "nir08" if "nir08" in assets else ("nir" if "nir" in assets else None)
                if nir is None:
                    raise RuntimeError("No se encontró banda NIR (nir08/nir).")
                if mode.upper() == "NDVI":
                    a1, a2 = nir, "red"
                else:
                    sw2 = "swir22" if "swir22" in assets else ("swir2" if "swir2" in assets else None)
                    if sw2 is None:
                        raise RuntimeError("No se encontró banda SWIR2 (swir22/swir2).")
                    a1, a2 = nir, sw2
                rlayer, vrt, buf = self._crop_assets_to_index_tif(assets, mode.upper(), a1, a2, "ls", layer_title)

            self._apply_index_colorramp(rlayer, mode.upper())
            group = "Índices (S2)" if kind == "s2" else "Índices (Landsat)"
            self._put_layer_in_group_add(rlayer, group)
            self._zoom_to_buffer(buf)
            self.lbl_status.setText(f"{mode.upper()} listo")
        except Exception as e:
            QMessageBox.critical(self, "Scene Browser", f"No se pudo generar {mode.upper()}:\n{e}")
            self.lbl_status.setText(f"Error {mode.upper()}")

    def on_ndvi_clicked(self):
        self._compute_index_for_last("NDVI")

    def on_nbr_clicked(self):
        self._compute_index_for_last("NBR")


    def _clear_grid(self):
        self._scene_buttons = []
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _cache_dir(self):
        p = os.path.join(QgsApplication.qgisSettingsDirPath(), "scene_browser_cache", "previews")
        os.makedirs(p, exist_ok=True)
        return p

    def _thumb_dir(self):
        p = os.path.join(QgsApplication.qgisSettingsDirPath(), "scene_browser_cache", "thumbs")
        os.makedirs(p, exist_ok=True)
        return p

    def _aoi_bbox_and_buffer(self):
        geom, crs = geom_from_xy(self.txt_x.text(), self.txt_y.text(), "EPSG:4326")
        buf5 = buffer_5km_epsg4326(geom, crs)
        buf3 = buffer_3km_epsg4326(geom, crs)
        self._last_buffer = buf5
        self._last_thumb_buffer = buf3
        rect = buf5.boundingBox()
        return [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]

    def _ensure_thumb_buffer(self):
        if self._last_thumb_buffer is None:
            raise RuntimeError("AOI no definido. Realiza una búsqueda primero.")
        return self._last_thumb_buffer

    def _ensure_buffer(self):
        if self._last_buffer is None:
            raise RuntimeError("AOI no definido. Realiza una búsqueda primero.")
        return self._last_buffer

    def _zoom_to_buffer(self, buf_geom4326):
        canvas = self.iface.mapCanvas()
        dest_crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        if dest_crs.authid() != "EPSG:4326":
            tr = QgsCoordinateTransform(wgs84, dest_crs, QgsProject.instance())
            g2 = QgsGeometry(buf_geom4326)
            g2.transform(tr)
            canvas.setExtent(g2.boundingBox())
        else:
            canvas.setExtent(buf_geom4326.boundingBox())

        canvas.refresh()

    def _sorted_features(self, data):
        feats = data.get("features", [])

        def _dt(f):
            p = f.get("properties", {})
            return p.get("datetime") or p.get("start_datetime") or ""
        feats.sort(key=_dt, reverse=True)
        return feats

    def _feature_label(self, f):
        sid = f.get("id", "")
        p = f.get("properties", {})
        dt = p.get("datetime") or p.get("start_datetime") or ""
        date = dt[:10] if dt else "---- -- --"
        return date, sid

    def _populate_list(self, feats, handler, thumb_kind):
        # ONE COLUMN
        row = 0
        for f in feats:
            date, sid = self._feature_label(f)

            btn = QToolButton()
            btn.setMinimumHeight(self._thumb_icon_px + 90)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(self._thumb_icon_px, self._thumb_icon_px))
            btn.setText(f"Fecha: {date}\nID: {sid}")
            btn.setAutoRaise(False)
            btn.setStyleSheet(
                "QToolButton{padding:10px; border-radius:12px; text-align:center;}"
            )
            btn.clicked.connect(partial(handler, f))

            self._scene_buttons.append(btn)

            # placeholder icon (gray)
            pm = QPixmap(self._thumb_icon_px, self._thumb_icon_px)
            pm.fill(Qt.lightGray)
            btn.setIcon(QIcon(pm))

            self.grid.addWidget(btn, row, 0)
            self._start_thumbnail(btn, f, thumb_kind)
            row += 1


    def _draw_point_marker(self, png_path: str, bbox4326, size_px: int):
        """Dibuja un punto amarillo (coordenada ingresada) sobre el thumbnail PNG."""
        try:
            x = float(self.txt_x.text().strip())
            y = float(self.txt_y.text().strip())
        except Exception:
            return

        xmin, ymin, xmax, ymax = bbox4326
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            return

        w = float(size_px)
        h = float(size_px)
        px = (x - xmin) / (xmax - xmin) * w
        py = (ymax - y) / (ymax - ymin) * h

        img = QImage(png_path)
        if img.isNull():
            return

        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(Qt.black)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.yellow))
        r = 6
        painter.drawEllipse(int(px) - r, int(py) - r, 2 * r, 2 * r)
        painter.end()
        img.save(png_path)

    def _start_thumbnail(self, btn: QToolButton, feature, kind: str):
        """Genera/aplica thumbnail real según la combinación seleccionada.

        El thumbnail depende del preset (Natural/Infrarrojo/Agricultura),
        por eso el cache incluye el preset para evitar reutilizar Natural.
        """
        try:
            buf = self._ensure_thumb_buffer()
            rect = buf.boundingBox()
            bbox = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())

            assets = feature.get("assets", {}) or {}
            preset = self.cmb_preset.currentText()

            if kind == "s2":
                rgb = s2_assets_for_preset(preset)
            else:
                rgb = [a for a in ls_assets_for_preset(preset, assets.keys()) if a]

            if not rgb or len(rgb) != 3 or any(a not in assets for a in rgb):
                return

            fid = (feature.get("id", "") or "unknown").replace("/", "_")
            preset_safe = preset.replace(" ", "_")
            try:
                cx = round(float(self.txt_x.text().strip()), 5)
                cy = round(float(self.txt_y.text().strip()), 5)
            except Exception:
                cx, cy = (0.0, 0.0)
            coord_safe = f"{cx}_{cy}".replace("-", "m").replace(".", "p")
            png_path = os.path.join(self._thumb_dir(), f"thumb_{kind}_{preset_safe}_{coord_safe}_{fid}.png")

            if os.path.exists(png_path):
                btn.setIcon(QIcon(png_path))
                return

            task = ThumbnailTask("Thumbnail…", self._thumb_dir(), assets, rgb, bbox, size_px=self._thumb_png_px)

            key = id(btn)
            self._thumb_tasks[key] = task

            def _apply_icon():
                try:
                    if task.png_path and os.path.exists(task.png_path):
                        try:
                            import shutil
                            shutil.copyfile(task.png_path, png_path)
                            png_path_local = png_path
                        except Exception:
                            png_path_local = task.png_path

                        try:
                            self._draw_point_marker(png_path_local, bbox, self._thumb_png_px)
                        except Exception:
                            pass
                        btn.setIcon(QIcon(png_path_local))
                    else:
                        if getattr(task, "error", None):
                            from qgis.core import QgsMessageLog, Qgis
                            QgsMessageLog.logMessage(f"Thumbnail no generado: {task.error}", "SceneBrowser", Qgis.Warning)
                finally:
                    self._thumb_tasks.pop(key, None)

            def _on_terminated():
                from qgis.core import QgsMessageLog, Qgis
                QgsMessageLog.logMessage("Thumbnail task terminated", "SceneBrowser", Qgis.Warning)
                self._thumb_tasks.pop(key, None)

            task.taskCompleted.connect(_apply_icon)
            task.taskTerminated.connect(_on_terminated)

            QgsApplication.taskManager().addTask(task)
        except Exception:
            return


    def refresh_scene_list(self):
        """Actualiza la lista (miniaturas) cuando cambia la combinación de bandas.
        No re-consulta STAC: solo re-renderiza con las escenas ya obtenidas.
        """
        if not getattr(self, "_last_feats", None) or not getattr(self, "_last_thumb_kind", None):
            return
        try:
            self._clear_grid()
            if self._last_thumb_kind == "s2":
                self._populate_list(self._last_feats, self.preview_s2, "s2")
            else:
                self._populate_list(self._last_feats, self.preview_landsat, "ls")
        except Exception:
            pass

    def search_scenes(self):
        if self.cmb_sensor.currentText().startswith("Sentinel-2"):
            return self.search_s2()
        return self.search_landsat()

    def search_s2(self):
        try:
            self._set_status_info("⏳Buscando Sentinel-2…")
            self._clear_grid()
            bbox = self._aoi_bbox_and_buffer()
            dt = stac_datetime_range(self.date_ini.date(), self.date_fin.date())
            limit = int(self.spin_limit.value())
            max_cloud = int(self.spin_cloud.value())

            query = {"eo:cloud_cover": {"lt": max_cloud}}

            data = self.stac_s2.search(
                collections=["sentinel-2-l2a"],
                bbox=bbox,
                datetime_range=dt,
                limit=limit,
                query=query
            )
            feats = self._sorted_features(data)
            self._last_feats = feats
            self._last_thumb_kind = "s2"
            self._populate_list(feats, self.preview_s2, "s2")
            self._set_status_ok(f"✔️Listo S2: {len(feats)} escenas (nubes < {max_cloud}%)")

        except Exception as e:
            self._set_status_error("❌Error al buscar escenas Sentinel-2")
           # QMessageBox.critical(self, "Error búsqueda (S2)", str(e))

    
    def _pc_get_token(self, collection_id: str) -> str:
        # Planetary Computer token vending (no credentials required)
        collection_id = (collection_id or "").strip()
        if not collection_id:
            return ""
        if collection_id in self._pc_token_cache:
            return self._pc_token_cache[collection_id]
        url = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection_id}"
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        token = r.json().get("token", "")
        self._pc_token_cache[collection_id] = token
        return token

    def _pc_sign_features(self, features, collection_id: str):
        token = self._pc_get_token(collection_id)
        if not token:
            return features
        for f in features:
            assets = f.get("assets") or {}
            for a in assets.values():
                href = a.get("href")
                if not href or "?" in href:
                    continue
                a["href"] = href + "?" + token
        return features

    def search_landsat(self):
        try:
            self._set_status_info("⏳Buscando Landsat…")
            self._clear_grid()
            bbox = self._aoi_bbox_and_buffer()
            dt = stac_datetime_range(self.date_ini.date(), self.date_fin.date())
            limit = int(self.spin_limit.value())
            max_cloud = int(self.spin_cloud.value())

            query = {"eo:cloud_cover": {"lt": max_cloud}}

            data = self.stac_ls.search(
                collections=["landsat-c2-l2"],
                bbox=bbox,
                datetime_range=dt,
                limit=limit,
                query=query
            )
            # Sign Landsat asset hrefs (Planetary Computer) so GDAL can read blobs
            if isinstance(data, dict) and 'features' in data:
                data['features'] = self._pc_sign_features(data.get('features', []), 'landsat-c2-l2')
            feats = self._sorted_features(data)
            self._last_feats = feats
            self._last_thumb_kind = "ls"
            self._populate_list(feats, self.preview_landsat, "ls")
            self._set_status_ok(f"✔️Listo Landsat: {len(feats)} escenas (nubes < {max_cloud}%)")

        except Exception as e:
            self._set_status_error("❌Error al buscar escenas Landsat")
            #QMessageBox.critical(self, "Error búsqueda (Landsat)", str(e))

    def _apply_index_colorramp(self, rlayer, mode: str):
        try:
            from qgis.core import QgsRasterShader, QgsColorRampShader, QgsSingleBandPseudoColorRenderer
            from qgis.PyQt.QtGui import QColor
            prov = rlayer.dataProvider()
            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Interpolated)
            mode_u = (mode or "").upper()
            if mode_u == "NDVI":
                items = [
                    QgsColorRampShader.ColorRampItem(-1.0, QColor(247, 252, 245)),
                    QgsColorRampShader.ColorRampItem(0.0, QColor(199, 233, 192)),
                    QgsColorRampShader.ColorRampItem(0.4, QColor(116, 196, 118)),
                    QgsColorRampShader.ColorRampItem(0.7, QColor(35, 139, 69)),
                    QgsColorRampShader.ColorRampItem(1.0, QColor(0, 109, 44)),
                ]
            else:
                items = [
                    QgsColorRampShader.ColorRampItem(-1.0, QColor(165, 0, 38)),
                    QgsColorRampShader.ColorRampItem(-0.4, QColor(215, 48, 39)),
                    QgsColorRampShader.ColorRampItem(0.0, QColor(247, 247, 247)),
                    QgsColorRampShader.ColorRampItem(0.6, QColor(135, 135, 135)),
                    QgsColorRampShader.ColorRampItem(1.0, QColor(77, 77, 77)),
                ]
            ramp.setColorRampItemList(items)
            shader.setRasterShaderFunction(ramp)
            renderer = QgsSingleBandPseudoColorRenderer(prov, 1, shader)
            rlayer.setRenderer(renderer)
            rlayer.triggerRepaint()
        except Exception:
            pass


    def _crop_assets_to_index_tif(self, assets_dict, mode: str, a1: str, a2: str, prefix: str, layer_title: str):
        """Recorta 2 assets (COG) al bbox del buffer y calcula un índice (NDVI/NBR).
        Re-muestrea la 2da banda a la grilla de la 1ra (p.ej. SWIR 20m vs NIR 10m).
        """
        buf = self._ensure_buffer()
        rect = buf.boundingBox()
        bbox = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())

        out_dir = self._cache_dir()
        uid = uuid.uuid4().hex

        def _crop_one(asset_name: str):
            href = assets_dict[asset_name]["href"]
            src = _src_from_href(href)
            out_tif = os.path.join(out_dir, f"{prefix}_{uid}_{asset_name}.tif")
            gdal.Translate(
                out_tif,
                src,
                options=gdal.TranslateOptions(
                    projWin=[bbox[0], bbox[3], bbox[2], bbox[1]],
                    projWinSRS="EPSG:4326",
                    format="GTiff",
                    creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
                ),
            )
            return out_tif

        # Fallback SWIR2 (algunas colecciones usan "swir2" en vez de "swir22")
        if mode.upper() == "NBR" and a2 not in assets_dict and "swir2" in assets_dict:
            a2 = "swir2"

        t1 = _crop_one(a1)
        t2 = _crop_one(a2)

        ds1 = gdal.Open(t1)
        ds2 = gdal.Open(t2)

        # Re-muestrear banda 2 a la grilla de ds1
        gt = ds1.GetGeoTransform()
        xsize = ds1.RasterXSize
        ysize = ds1.RasterYSize
        xmin = gt[0]
        ymax = gt[3]
        xmax = xmin + gt[1] * xsize
        ymin = ymax + gt[5] * ysize

        t2m = os.path.join(out_dir, f"{prefix}_{uid}_{a2}_match.tif")
        gdal.Warp(
            t2m,
            ds2,
            options=gdal.WarpOptions(
                format="GTiff",
                dstSRS=ds1.GetProjection(),
                outputBounds=[xmin, ymin, xmax, ymax],
                width=xsize,
                height=ysize,
                resampleAlg="bilinear",
                creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
            ),
        )
        ds2m = gdal.Open(t2m)

        b1 = ds1.GetRasterBand(1).ReadAsArray().astype("float32")
        b2 = ds2m.GetRasterBand(1).ReadAsArray().astype("float32")

        # ✅ Apply reflectance scaling ONLY for Landsat C2 L2
        # Sentinel-2 assets from EarthSearch are already reflectance-like; do not scale them.
        if prefix.lower() == "ls":
            b1 = b1 * LS_SR_MULT + LS_SR_ADD
            b2 = b2 * LS_SR_MULT + LS_SR_ADD

        denom = (b1 + b2)

        with np.errstate(divide="ignore", invalid="ignore"):
            idx = (b1 - b2) / denom

        # Clean non-finite and (optional) hard clip to [-1, 1] to match expected index domain
        idx[~np.isfinite(idx)] = -9999.0
        idx = np.where(idx == -9999.0, -9999.0, np.clip(idx, -1.0, 1.0)).astype("float32")

        out_idx = os.path.join(out_dir, f"{prefix}_{uid}_{mode}.tif")
        drv = gdal.GetDriverByName("GTiff")
        out_ds = drv.Create(
            out_idx,
            xsize,
            ysize,
            1,
            gdal.GDT_Float32,
            options=["TILED=YES", "COMPRESS=DEFLATE"],
        )
        out_ds.SetGeoTransform(ds1.GetGeoTransform())
        out_ds.SetProjection(ds1.GetProjection())
        ob = out_ds.GetRasterBand(1)
        ob.WriteArray(idx)
        ob.SetNoDataValue(-9999.0)
        ob.FlushCache()
        out_ds.FlushCache()
        out_ds = None

        rlayer = QgsRasterLayer(out_idx, layer_title)
        if not rlayer.isValid():
            raise RuntimeError("No se pudo crear la capa de índice")
        return rlayer, out_idx, buf


    def _crop_assets_to_vrt(self, assets_dict, asset_names, prefix, layer_title):
        buf = self._ensure_buffer()
        rect = buf.boundingBox()
        bbox = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())

        out_dir = self._cache_dir()
        uid = uuid.uuid4().hex
        tifs = []

        for a in asset_names:
            href = assets_dict[a]["href"]
            src = _src_from_href(href)
            out_tif = os.path.join(out_dir, f"{prefix}_{uid}_{a}.tif")
            gdal.Translate(
                out_tif,
                src,
                options=gdal.TranslateOptions(
                    projWin=[bbox[0], bbox[3], bbox[2], bbox[1]],
                    projWinSRS="EPSG:4326",
                    format="GTiff",
                    creationOptions=["TILED=YES", "COMPRESS=DEFLATE"]
                )
            )
            tifs.append(out_tif)

        out_vrt = os.path.join(out_dir, f"{prefix}_{uid}.vrt")
        gdal.BuildVRT(out_vrt, tifs, separate=True)

        rlayer = QgsRasterLayer(out_vrt, layer_title)
        if not rlayer.isValid():
            raise RuntimeError("No se pudo cargar el VRT en QGIS.")
        return rlayer, out_vrt, buf

    def _apply_stretch_async(self, rlayer, vrt_path, done_text):
        task = PercentileStretchTask("Stretch 2–98…", vrt_path, p_low=2, p_high=98)

        def _apply():
            if not task.result:
                self.lbl_status.setText(done_text + " (sin stretch)")
                return

            prov = rlayer.dataProvider()
            renderer = QgsMultiBandColorRenderer(prov, 1, 2, 3)

            def _ce(band_no, vmin, vmax):
                ce = QgsContrastEnhancement(prov.dataType(band_no))
                ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum, True)
                ce.setMinimumValue(float(vmin))
                ce.setMaximumValue(float(vmax))
                return ce

            (rmin, rmax) = task.result["r"]
            (gmin, gmax) = task.result["g"]
            (bmin, bmax) = task.result["b"]
            renderer.setRedContrastEnhancement(_ce(1, rmin, rmax))
            renderer.setGreenContrastEnhancement(_ce(2, gmin, gmax))
            renderer.setBlueContrastEnhancement(_ce(3, bmin, bmax))

            rlayer.setRenderer(renderer)
            rlayer.triggerRepaint()
            self.lbl_status.setText(done_text + " ✓")

        task.taskCompleted.connect(_apply)
        QgsApplication.taskManager().addTask(task)

    def preview_s2(self, feature):
        try:
            self.lbl_status.setText("Creando preview S2 (5 km)…")
            self._last_selected_feature = feature
            self._last_selected_kind = "s2"
            self.btn_ndvi.setEnabled(True)
            self.btn_nbr.setEnabled(True)
            preset = self.cmb_preset.currentText()
            assets_need = s2_assets_for_preset(preset)

            assets = feature.get("assets", {}) or {}
            missing = [a for a in assets_need if not (isinstance(a, str) and a.startswith('__')) and a not in assets]
            if missing:
                raise RuntimeError(f"Faltan assets {missing}. Disponibles: {list(assets.keys())[:16]}...")

            title = f"S2 {preset} | {feature.get('id','')}"
            if assets_need and str(assets_need[0]).startswith("__"):
                mode = assets_need[0].strip("_")
                rlayer, vrt, buf = self._crop_assets_to_index_tif(assets, mode, assets_need[1], assets_need[2], "s2", title)
            else:
                rlayer, vrt, buf = self._crop_assets_to_vrt(assets, assets_need, "s2", title)

            self._put_layer_in_group_add(rlayer, "Previews S2")
            self._zoom_to_buffer(buf)
            if isinstance(vrt, str) and vrt.lower().endswith('.vrt'):
                self._apply_stretch_async(rlayer, vrt, "Preview S2 listo")
            else:
                self.lbl_status.setText("Preview S2 listo ✓")
        except Exception as e:
            self.lbl_status.setText("")
            QMessageBox.critical(self, "Error preview (S2)", str(e))

    def preview_landsat(self, feature):
        try:
            self.lbl_status.setText("Creando preview Landsat (5 km)…")
            self._last_selected_feature = feature
            self._last_selected_kind = "ls"
            self.btn_ndvi.setEnabled(True)
            self.btn_nbr.setEnabled(True)
            preset = self.cmb_preset.currentText()

            assets = feature.get("assets", {}) or {}
            available = set(assets.keys())
            assets_need = [a for a in ls_assets_for_preset(preset, available) if a]
            if len(assets_need) != 3:
                raise RuntimeError(f"No pude determinar 3 assets para {preset}. Disponibles: {list(available)[:20]}...")

            missing = [a for a in assets_need if not (isinstance(a, str) and a.startswith('__')) and a not in assets]
            if missing:
                raise RuntimeError(f"Faltan assets {missing}. Disponibles: {list(available)[:20]}...")

            title = f"Landsat {preset} | {feature.get('id','')}"
            if assets_need and str(assets_need[0]).startswith("__"):
                mode = assets_need[0].strip("_")
                rlayer, vrt, buf = self._crop_assets_to_index_tif(assets, mode, assets_need[1], assets_need[2], "ls", title)
            else:
                rlayer, vrt, buf = self._crop_assets_to_vrt(assets, assets_need, "ls", title)

            self._put_layer_in_group_add(rlayer, "Previews Landsat")
            self._zoom_to_buffer(buf)
            if isinstance(vrt, str) and vrt.lower().endswith('.vrt'):
                self._apply_stretch_async(rlayer, vrt, "Preview Landsat listo")
            else:
                self.lbl_status.setText("Preview Landsat listo ✓")
        except Exception as e:
            self.lbl_status.setText("")
            QMessageBox.critical(self, "Error preview (Landsat)", str(e))

    def _put_layer_in_group_add(self, layer, group_name: str):
        proj = QgsProject.instance()
        root = proj.layerTreeRoot()
        group = root.findGroup(group_name)
        if group is None:
            group = root.addGroup(group_name)

        proj.addMapLayer(layer, False)
        group.insertLayer(0, layer)
