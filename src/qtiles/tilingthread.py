# ******************************************************************************
#
# QTiles
# ---------------------------------------------------------
# Generates tiles from QGIS project
#
# Copyright (C) 2012-2022 NextGIS (info@nextgis.com)
#
# This source is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 2 of the License, or (at your option)
# any later version.
#
# This code is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# A copy of the GNU General Public License is available on the World Wide Web
# at <http://www.gnu.org/licenses/>. You can also obtain it by writing
# to the Free Software Foundation, 51 Franklin Street, Suite 500 Boston,
# MA 02110-1335 USA.
#
# ******************************************************************************
import codecs
import json
import time
from string import Template

from qgis.core import (
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsMessageLog,
    QgsProject,
    QgsScaleCalculator,
    QgsGeometry,
    QgsMapToPixel,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QFile, QIODevice, QMutex, Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QPainter, QImage, QBrush, QColor, QPainterPath
from qgis.PyQt.QtWidgets import *

from . import resources_rc  # noqa: F401
from .compat import (
    QGIS_VERSION_3,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLogInfo,
)
from .tile import Tile
from .writers import *


def printQtilesLog(msg, level=QgsMessageLogInfo):
    QgsMessageLog.logMessage(msg, "QTiles", level)


class TilingThread(QThread):
    rangeChanged = pyqtSignal(str, int)
    updateProgress = pyqtSignal()
    processFinished = pyqtSignal()
    processInterrupted = pyqtSignal()
    threshold = pyqtSignal(int)

    warring_threshold_tiles_count = 10000

    def __init__(
        self,
        layers,
        extent,
        minZoom,
        maxZoom,
        width,
        height,
        transp,
        quality,
        format,
        outputPath,
        rootDir,
        antialiasing,
        tmsConvention,
        mbtilesCompression,
        jsonFile,
        overview,
        renderOutsideTiles,
        mapUrl,
        viewer,
        polygon,
        usePolygonMask,
        renderBoundaries="all"
    ):
        QThread.__init__(self, QThread.currentThread())
        self.mutex = QMutex()
        self.confirmMutex = QMutex()
        self.stopMe = 0
        self.interrupted = False
        self.layers = layers
        self.extent = extent
        self.minZoom = minZoom
        self.maxZoom = maxZoom
        self.output = outputPath
        self.width = width
        if rootDir:
            self.rootDir = rootDir
        else:
            self.rootDir = "tileset_%s" % str(time.time()).split(".")[0]
        self.antialias = antialiasing
        self.tmsConvention = tmsConvention
        self.mbtilesCompression = mbtilesCompression
        self.format = format
        self.quality = quality
        self.jsonFile = jsonFile
        self.overview = overview
        self.renderOutsideTiles = renderOutsideTiles
        self.mapurl = mapUrl
        self.viewer = viewer
        self.polygon = polygon
        self.usePolygonMask = usePolygonMask
        self.renderBoundaries = renderBoundaries
        if self.output.isDir():
            self.mode = "DIR"
        elif self.output.suffix().lower() == "zip":
            self.mode = "ZIP"
        elif self.output.suffix().lower() == "ngrc":
            self.mode = "NGM"
        elif self.output.suffix().lower() == "mbtiles":
            self.mode = "MBTILES"
            self.tmsConvention = True
        self.interrupted = False
        self.tiles = []
        self.layersId = []
        for layer in self.layers:
            self.layersId.append(layer.id())
        myRed = QgsProject.instance().readNumEntry(
            "Gui", "/CanvasColorRedPart", 255
        )[0]
        myGreen = QgsProject.instance().readNumEntry(
            "Gui", "/CanvasColorGreenPart", 255
        )[0]
        myBlue = QgsProject.instance().readNumEntry(
            "Gui", "/CanvasColorBluePart", 255
        )[0]
        self.color = QColor(myRed, myGreen, myBlue, transp)
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        self.projector = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem.fromEpsgId(4326),
            QgsCoordinateReferenceSystem.fromEpsgId(3395),
        )
        self.scaleCalc = QgsScaleCalculator()
        self.scaleCalc.setDpi(image.logicalDpiX())
        self.scaleCalc.setMapUnits(
            QgsCoordinateReferenceSystem.fromEpsgId(3395).mapUnits()
        )
        self.settings = QgsMapSettings()
        self.settings.setBackgroundColor(self.color)

        if not QGIS_VERSION_3:
            self.settings.setCrsTransformEnabled(True)

        self.settings.setOutputDpi(image.logicalDpiX())
        self.settings.setOutputImageFormat(QImage.Format_ARGB32_Premultiplied)
        self.settings.setDestinationCrs(
            QgsCoordinateReferenceSystem.fromEpsgId(3395)
        )
        self.settings.setOutputSize(image.size())

        if QGIS_VERSION_3:
            self.settings.setLayers(self.layers)
        else:
            self.settings.setLayers(self.layersId)

        if not QGIS_VERSION_3:
            self.settings.setMapUnits(
                QgsCoordinateReferenceSystem.fromEpsgId(3395).mapUnits()
            )

        if self.antialias:
            self.settings.setFlag(QgsMapSettings.Antialiasing, True)
        else:
            self.settings.setFlag(QgsMapSettings.DrawLabeling, True)

    def run(self):
        self.mutex.lock()
        self.stopMe = 0
        self.mutex.unlock()
        if self.mode == "DIR":
            self.writer = DirectoryWriter(self.output, self.rootDir)
            if self.mapurl:
                self.writeMapurlFile()
            if self.viewer:
                self.writeLeafletViewer()
        elif self.mode == "ZIP":
            self.writer = ZipWriter(self.output, self.rootDir)
        elif self.mode == "NGM":
            self.writer = NGMArchiveWriter(self.output, self.rootDir)
        elif self.mode == "MBTILES":
            self.writer = MBTilesWriter(
                self.output,
                self.rootDir,
                self.format,
                self.minZoom,
                self.maxZoom,
                self.extent,
                self.mbtilesCompression,
            )
        if self.jsonFile:
            self.writeJsonFile()
        if self.overview:
            self.writeOverviewFile()
        self.rangeChanged.emit(self.tr("Searching tiles..."), 0)
        useTMS = 1
        if self.tmsConvention:
            useTMS = -1
        self.countTiles(Tile(0, 0, 0, useTMS))

        if self.interrupted:
            del self.tiles[:]
            self.tiles = None
            self.processInterrupted.emit()
        self.rangeChanged.emit(
            self.tr("Rendering: %v from %m (%p%)"), len(self.tiles)
        )

        if len(self.tiles) > self.warring_threshold_tiles_count:
            self.confirmMutex.lock()
            self.threshold.emit(self.warring_threshold_tiles_count)

        self.confirmMutex.lock()
        if self.interrupted:
            self.processInterrupted.emit()
            return

        for t in self.tiles:
            self.render(t, self.polygon)
            self.updateProgress.emit()
            self.mutex.lock()
            s = self.stopMe
            self.mutex.unlock()
            if s == 1:
                self.interrupted = True
                break

        self.writer.finalize()
        if not self.interrupted:
            self.processFinished.emit()
        else:
            self.processInterrupted.emit()

    def stop(self):
        self.mutex.lock()
        self.stopMe = 1
        self.mutex.unlock()
        QThread.wait(self)

    def confirmContinue(self):
        self.confirmMutex.unlock()

    def confirmStop(self):
        self.interrupted = True
        self.confirmMutex.unlock()

    def writeJsonFile(self):
        filePath = "%s.json" % self.output.absoluteFilePath()
        if self.mode == "DIR":
            filePath = "%s/%s.json" % (
                self.output.absoluteFilePath(),
                self.rootDir,
            )
        info = {
            "name": self.rootDir,
            "format": self.format.lower(),
            "minZoom": self.minZoom,
            "maxZoom": self.maxZoom,
            "bounds": str(self.extent.xMinimum())
            + ","
            + str(self.extent.yMinimum())
            + ","
            + str(self.extent.xMaximum())
            + ","
            + str(self.extent.yMaximum()),
        }
        with open(filePath, "w") as f:
            f.write(json.dumps(info))

    def writeOverviewFile(self):
        self.settings.setExtent(self.projector.transform(self.extent))

        image = QImage(self.settings.outputSize(), QImage.Format_ARGB32)
        image.fill(Qt.transparent)

        dpm = round(self.settings.outputDpi() / 25.4 * 1000)
        image.setDotsPerMeterX(dpm)
        image.setDotsPerMeterY(dpm)

        # job = QgsMapRendererSequentialJob(self.settings)
        # job.start()
        # job.waitForFinished()
        # image = job.renderedImage()

        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(self.settings, painter)
        job.renderSynchronously()
        painter.end()

        filePath = "%s.%s" % (
            self.output.absoluteFilePath(),
            self.format.lower(),
        )
        if self.mode == "DIR":
            filePath = "%s/%s.%s" % (
                self.output.absoluteFilePath(),
                self.rootDir,
                self.format.lower(),
            )
        image.save(filePath, self.format, self.quality)

    def writeMapurlFile(self):
        filePath = "%s/%s.mapurl" % (
            self.output.absoluteFilePath(),
            self.rootDir,
        )
        tileServer = "tms" if self.tmsConvention else "google"
        with open(filePath, "w") as mapurl:
            mapurl.write(
                "%s=%s\n" % ("url", self.rootDir + "/ZZZ/XXX/YYY.png")
            )
            mapurl.write("%s=%s\n" % ("minzoom", self.minZoom))
            mapurl.write("%s=%s\n" % ("maxzoom", self.maxZoom))
            mapurl.write(
                "%s=%f %f\n"
                % (
                    "center",
                    self.extent.center().x(),
                    self.extent.center().y(),
                )
            )
            mapurl.write("%s=%s\n" % ("type", tileServer))

    def writeLeafletViewer(self):
        templateFile = QFile(":/plugins/qtiles/resources/viewer.html")
        if templateFile.open(QIODevice.ReadOnly | QIODevice.Text):
            viewer = MyTemplate(str(templateFile.readAll()))

            tilesDir = "%s/%s" % (self.output.absoluteFilePath(), self.rootDir)
            useTMS = "true" if self.tmsConvention else "false"
            substitutions = {
                "tilesdir": tilesDir,
                "tilesext": self.format.lower(),
                "tilesetname": self.rootDir,
                "tms": useTMS,
                "centerx": self.extent.center().x(),
                "centery": self.extent.center().y(),
                "avgzoom": (self.maxZoom + self.minZoom) / 2,
                "maxzoom": self.maxZoom,
            }

            filePath = "%s/%s.html" % (
                self.output.absoluteFilePath(),
                self.rootDir,
            )
            with codecs.open(filePath, "w", "utf-8") as fOut:
                fOut.write(viewer.substitute(substitutions))
            templateFile.close()

    def countTiles(self, tile):
        if self.interrupted or not self.extent.intersects(tile.toRectangle()):
            return
        if self.minZoom <= tile.z and tile.z <= self.maxZoom:
            """if not self.renderOutsideTiles:
                for layer in self.layers:
                    tile_rectangle = tile.toRectangle()
                    t = QgsCoordinateTransform(
                        layer.crs(),
                        QgsCoordinateReferenceSystem.fromEpsgId(4326),
                    )
                    if t.transform(layer.extent()).intersects(
                        tile.toRectangle()
                        tile_rectangle
                    ):
                        self.tiles.append(tile)
                        if self.polygon:
                            intersects = self.polygon.intersects(tile_rectangle)
                            if intersects:
                                self.tiles.append(tile)
                        else:
                            self.tiles.append(tile)
                        break"""
            if not self.renderOutsideTiles:
                for layer in self.layers:
                    t = QgsCoordinateTransform(
                        layer.crs(),
                        QgsCoordinateReferenceSystem.fromEpsgId(4326),
                    )
                    if t.transform(layer.extent()).intersects(
                        tile.toRectangle()
                    ):
                        self.tiles.append(tile)
                        break
            
            else:
                self.tiles.append(tile)
        if tile.z < self.maxZoom:
            for x in range(2 * tile.x, 2 * tile.x + 2, 1):
                for y in range(2 * tile.y, 2 * tile.y + 2, 1):
                    self.mutex.lock()
                    s = self.stopMe
                    self.mutex.unlock()
                    if s == 1:
                        self.interrupted = True
                        return
                    subTile = Tile(x, y, tile.z + 1, tile.tms)
                    self.countTiles(subTile)

    
    @staticmethod
    def mask_image_fast(settings, image, polygon):
        """
        settings: текущий QgsMapSettings из вашего класса
        image: QImage тайла
        polygon: QgsGeometry (маска в WGS84)
        """
        # 1. Создаем CRS (используем безопасный метод)
        poly_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326) # Маска обычно в 4326
        dest_crs = settings.destinationCrs()

        # 2. Исправленный конструктор трансформации (только 2 аргумента)
        # В QGIS 3.34 для стабильности лучше использовать только SRC и DEST
        # xform = QgsCoordinateTransform(poly_crs, dest_crs, QgsProject.instance()) 
        # Если снова будет ошибка про 4 аргумента, замените строку выше на:
        xform = QgsCoordinateTransform(poly_crs, dest_crs)

        # 3. Трансформируем маску
        mask_geom = QgsGeometry(polygon)
        mask_geom.transform(xform)

        # 4. Получаем границы тайла (Extent)
        extent = settings.extent()
        tile_geom = QgsGeometry.fromRect(extent)

        # Находим пересечение
        intersection = mask_geom.intersection(tile_geom)

        # Если пересечения нет — тайл пустой
        if intersection.isEmpty():
            image.fill(Qt.transparent)
            return image

        # !!! ГЛАВНОЕ ИСПРАВЛЕНИЕ: Проверяем тип геометрии !!!
        # Нас интересуют только площади (Polygon / MultiPolygon). 
        # Если пересечение — это точка или линия, значит площади пересечения нет.
        if intersection.type() != QgsWkbTypes.PolygonGeometry:
            image.fill(Qt.transparent)
            return image

        # 5. Создаем маску-холст
        mask = QImage(image.size(), QImage.Format_ARGB32)
        mask.fill(Qt.transparent)

        # 6. Используем MapToPixel (лучший способ синхронизации ГИС и Пикселей)
        m2p = QgsMapToPixel(
            settings.mapUnitsPerPixel(),
            extent.center().x(),
            extent.center().y(),
            image.width(),
            image.height(),
            settings.rotation()
        )

        painter = QPainter(mask)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(0, 0, 0)) # Цвет любой, важна альфа
        painter.setPen(Qt.NoPen)

        # 7. Преобразуем геометрию в путь QPainterPath
        # Обрабатываем и MultiPolygon и обычный Polygon
        parts = intersection.asMultiPolygon() if intersection.isMultipart() else [intersection.asPolygon()]
        
        for poly in parts:
            path = QPainterPath()
            for ring in poly:
                for i, pt in enumerate(ring):
                    # Точная конвертация координат карты в пиксели картинки
                    pixel_pt = m2p.transform(pt.x(), pt.y())
                    if i == 0:
                        path.moveTo(pixel_pt.x(), pixel_pt.y())
                    else:
                        path.lineTo(pixel_pt.x(), pixel_pt.y())
                path.closeSubpath()
            painter.drawPath(path)
        
        painter.end()

        # 8. Накладываем маску на исходный тайл
        final_painter = QPainter(image)
        final_painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        final_painter.drawImage(0, 0, mask)
        final_painter.end()

        return image



    def render(self, tile, polygon=None):
        # scale = self.scaleCalc.calculate(
        #    self.projector.transform(tile.toRectangle()), self.width)

        self.settings.setExtent(self.projector.transform(tile.toRectangle()))
        tile_rectangle = tile.toRectangle()
        
        # Determine whether to skip the tile based on renderBoundaries setting
        if polygon:
            intersects = polygon.intersects(tile_rectangle)
            tile_geometry = QgsGeometry.fromRect(tile_rectangle)
            contains = polygon.contains(tile_geometry)
            
            if self.renderBoundaries == "intersects":
                if not intersects or contains:
                    return  # Skip rendering if no intersection and fully contained
            elif self.renderBoundaries == "contains":
                if not contains:
                    return  # Skip rendering if not fully contained
            elif self.renderBoundaries == "all":
                if not intersects:
                    return  # Skip rendering if no intersection

        image = QImage(self.settings.outputSize(), QImage.Format_ARGB32)
        image.fill(Qt.transparent)

        dpm = round(self.settings.outputDpi() / 25.4 * 1000)
        image.setDotsPerMeterX(dpm)
        image.setDotsPerMeterY(dpm)

        # job = QgsMapRendererSequentialJob(self.settings)
        # job.start()
        # job.waitForFinished()
        # image = job.renderedImage()

        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(self.settings, painter)
        job.renderSynchronously()
        painter.end()
        
        # Apply polygon masking if enabled and polygon exists
        if self.usePolygonMask and polygon:
            if contains:
                # If the tile is completely within the polygon, we can skip masking
                pass
            else:
                image = self.mask_image_fast(self.settings, image, polygon)
        
        self.writer.writeTile(tile, image, self.format, self.quality)


class MyTemplate(Template):
    delimiter = "@"

    def __init__(self, templateString):
        Template.__init__(self, templateString)
