from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
import os
from .gui.dockwidget import SceneBrowserDock


class SceneBrowserPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None

    def initGui(self):
        self.action = QAction(QIcon(os.path.join(os.path.dirname(__file__), "icon.png")), "Visor de escenas Sentinel 2 y Landsat -Sernanp", self.iface.mainWindow())
        self.action.triggered.connect(self.open_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Visor de escenas-SERNANP", self.action)

    def unload(self):
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&Visor de escenas-SERNANP", self.action)
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock = None

    def open_dock(self):
        if not self.dock:
            self.dock = SceneBrowserDock(self.iface)
            self.iface.addDockWidget(2, self.dock)  # Right dock area
        self.dock.show()
        self.dock.raise_()
