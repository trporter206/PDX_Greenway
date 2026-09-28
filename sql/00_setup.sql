-- Run once, connected to the pdx_greenway database:
--   createdb pdx_greenway
--   psql -d pdx_greenway -f sql/00_setup.sql

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- raw:      data exactly as downloaded (never edited by hand)
-- network:  the conflated, scored routing network
-- analysis: routing outputs, corridors, scenarios
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS network;
CREATE SCHEMA IF NOT EXISTS analysis;

SELECT postgis_full_version();
SELECT pgr_version();
