def classFactory(iface):
    from .plugin import SceneBrowserPlugin
    return SceneBrowserPlugin(iface)
