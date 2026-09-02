from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import overturemaps
import json

app = FastAPI()

# CRITICAL FIX: allow_credentials MUST be False when using allow_origins=["*"]
# Otherwise, the browser accepts the OPTIONS check but completely blocks the POST request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

class PolygonRequest(BaseModel):
    geometry: dict

@app.post("/api/detect-buildings")
async def detect_buildings(request: PolygonRequest):
    try:
        print("Received polygon from map. Calculating bounding box...")
        coords = request.geometry.get('coordinates', [[]])[0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        
        # Overture Maps requires bbox as (xmin, ymin, xmax, ymax)
        xmin, ymin, xmax, ymax = min(lons), min(lats), max(lons), max(lats)
        print(f"Fetching AI buildings for bbox: {xmin}, {ymin}, {xmax}, {ymax}")

        # Use the official Overture library to fetch Google/MS AI footprints instantly
        # This prevents RAM crashes on Render's free tier
        gdf = overturemaps.get_features(bbox=(xmin, ymin, xmax, ymax), theme="buildings", type="building")
        
        print(f"Successfully extracted {len(gdf)} households. Converting to GeoJSON...")
        
        geojson_str = gdf.to_json()
        geojson_dict = json.loads(geojson_str)
        
        print("Sending household coordinates back to the frontend...")
        return geojson_dict
        
    except Exception as e:
        print(f"Backend Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
