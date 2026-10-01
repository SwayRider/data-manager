"""Registers the stages that exist. Import this module before looking a stage up by key."""
from datamanager.stages.border import BorderStage
from datamanager.stages.download_osm import DownloadOsmStage
from datamanager.stages.download_planet import DownloadPlanetStage
from datamanager.stages.download_srtm import DownloadSrtmStage
from datamanager.stages.download_tiles import DownloadTilesStage
from datamanager.stages.extract_countries import ExtractCountriesStage
from datamanager.stages.osm_extract import OsmExtractStage
from datamanager.stages.noop import NoOpStage
from datamanager.stages.polygons import PolygonsStage
from datamanager.stages.registry import default_registry
from datamanager.stages.styles import StylesStage

for _stage in (NoOpStage, DownloadOsmStage, PolygonsStage, OsmExtractStage, DownloadPlanetStage, ExtractCountriesStage,
               DownloadTilesStage, StylesStage, DownloadSrtmStage, BorderStage):
    default_registry.register(_stage)
