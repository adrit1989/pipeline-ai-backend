from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import duckdb
import json

app = FastAPI()

# CRITICAL: Allow Google Apps Script to communicate with this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PolygonRequest(BaseModel):
    geometry: dict

# Initialize DuckDB and load spatial extensions
@app.on_event("startup")
def startup_event():
    global con
    con = duckdb.connect(database=':memory:')
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")

@app.post("/api/detect-buildings")
async def detect_buildings(request: PolygonRequest):
    try:
        # 1. Extract bounding box from the drawn polygon
        coords = request.geometry.get('coordinates', [[]])[0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        xmin, xmax = min(lons), max(lons)
        ymin, ymax = min(lats), max(lats)

        # 2. Query Google Open Buildings / Overture Maps via DuckDB
        query = f"""
            SELECT ST_AsGeoJSON(geometry) as geojson 
            FROM read_parquet('s3://overturemaps-us-west-2/release/2026-08-19.0/theme=buildings/type=building/*.parquet')
            WHERE bbox.xmin > {xmin} AND bbox.xmax < {xmax} 
            AND bbox.ymin > {ymin} AND bbox.ymax < {ymax}
        """
        
        # 3. Execute and format results
        results = con.execute(query).fetchall()
        
        features = []
        for row in results:
            geom = json.loads(row[0])
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {}
            })
            
        return {"type": "FeatureCollection", "features": features}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "AI Infrastructure API is running"}
