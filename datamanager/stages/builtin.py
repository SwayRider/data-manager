"""Registers the stages that exist. Import this module before looking a stage up by key."""
from datamanager.stages.border import BorderStage
from datamanager.stages.download_osm import DownloadOsmStage
from datamanager.stages.download_pelias import DownloadPeliasStage
from datamanager.stages.download_planet import DownloadPlanetStage
from datamanager.stages.download_srtm import DownloadSrtmStage
from datamanager.stages.download_tiles import DownloadTilesStage
from datamanager.stages.extract_countries import ExtractCountriesStage
from datamanager.stages.osm_extract import OsmExtractStage
from datamanager.stages.noop import NoOpStage
from datamanager.stages.pelias import PeliasStage
from datamanager.stages.pelias_interpolation import PeliasInterpolationStage
from datamanager.stages.polygons import PolygonsStage
from datamanager.stages.registry import default_registry
from datamanager.stages.styles import StylesStage
from datamanager.stages.valhalla import ValhallaStage
from datamanager.stages.wof_patch import WofPatchStage

for _stage in (NoOpStage, DownloadOsmStage, PolygonsStage, OsmExtractStage, DownloadPlanetStage, ExtractCountriesStage,
               DownloadTilesStage, StylesStage, DownloadSrtmStage, BorderStage, ValhallaStage, DownloadPeliasStage, WofPatchStage, PeliasStage,
               PeliasInterpolationStage):
    default_registry.register(_stage)
