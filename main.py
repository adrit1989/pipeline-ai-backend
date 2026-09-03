from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import duckdb
import json

app = FastAPI()

# Required so the browser doesn't block the connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

class PolygonRequest(BaseModel):
    geometry: dict

# Initialize DuckDB with strict memory limits so Render's free tier doesn't crash
@app.on_event("startup")
def startup_event():
    global con
    con = duckdb.connect(database=':memory:')
    con.execute("PRAGMA threads=2;")
    con.execute("PRAGMA memory_limit='256MB';")
    con.execute("INSTALL spatial; LOAD spatial;")
    # Removed httpfs and S3 settings because we are reading local files now!

@app.post("/api/detect-buildings")
async def detect_buildings(request: PolygonRequest):
    try:
        print("Received polygon from map. Calculating bounding box...")
        coords = request.geometry.get('coordinates', [[]])[0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        
        xmin, xmax = min(lons), max(lons)
        ymin, ymax = min(lats), max(lats)
        
        print(f"Fetching buildings for bbox: {xmin}, {ymin}, {xmax}, {ymax}")

        # Connect DIRECTLY to the local chunked parquet folders using a wildcard
        query = f"""
            SELECT ST_AsGeoJSON(geometry) as geojson 
            FROM read_parquet('*_split/*.parquet')
            WHERE bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} 
            AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}
            LIMIT 5000
        """
        
        results = con.execute(query).fetchall()
        print(f"Successfully extracted {len(results)} households. Converting to GeoJSON...")
        
        features = []
        for row in results:
            geom = json.loads(row[0])
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {}
            })
            
        print("Sending household coordinates back to the frontend...")
        return {"type": "FeatureCollection", "features": features}
        
    except Exception as e:
        print(f"Backend Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
