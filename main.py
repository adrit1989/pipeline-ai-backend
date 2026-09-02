from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import duckdb
import json
import urllib.request
import urllib.error
import base64

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

class PolygonRequest(BaseModel):
    geometry: dict
    password: str = ""
    geminiKey: str = ""

@app.on_event("startup")
def startup_event():
    global con
    con = duckdb.connect(database=':memory:')
    con.execute("PRAGMA threads=2;")
    con.execute("PRAGMA memory_limit='256MB';")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")

def call_gemini_vision(image_data: bytes, api_key: str):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    b64_img = base64.b64encode(image_data).decode('utf-8')
    
    payload = {
        "contents": [{
            "parts": [
                {"text": "You are a GIS AI. Return a JSON array of bounding boxes for every house, roof, and hut in this aerial image. Format strictly like this: [{\"ymin\": 0.1, \"xmin\": 0.2, \"ymax\": 0.15, \"xmax\": 0.25}]. Coordinates MUST be normalized from 0.0 to 1.0 (top-left to bottom-right). Return ONLY the raw JSON array without markdown headers or backticks."},
                {"inlineData": {"mimeType": "image/jpeg", "data": b64_img}}
            ]
        }]
    }
    
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            return res_data['candidates'][0]['content']['parts'][0]['text']
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        raise Exception(f"HTTP {e.code}: {error_body}")

@app.post("/api/detect-buildings")
async def detect_buildings(request: PolygonRequest):
    try:
        if request.password != "501312":
            raise HTTPException(status_code=401, detail="Unauthorized")

        coords = request.geometry.get('coordinates', [[]])[0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        
        xmin, xmax = min(lons), max(lons)
        ymin, ymax = min(lats), max(lats)
        
        features = []
        
        # --- PASS 1: Overture Maps AI Database ---
        try:
            query = f"""
                SELECT ST_AsGeoJSON(geometry) as geojson 
                FROM read_parquet('s3://overturemaps-us-west-2/release/2026-08-19.0/theme=buildings/type=building/*', filename=true, hive_partitioning=1)
                WHERE bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} 
                AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}
                LIMIT 5000
            """
            results = con.execute(query).fetchall()
            for row in results:
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(row[0]),
                    "properties": {"source": "Overture"}
                })
        except Exception as e:
            print("Overture Maps Fetch Error:", str(e))

        # --- PASS 2: Gemini 1.5 Flash Deep Scan ---
        if request.geminiKey:
            try:
                # 1. Create a perfectly square bounding box so ESRI doesn't crash from distortion
                width = xmax - xmin
                height = ymax - ymin
                max_dim = max(width, height)
                
                if max_dim == 0: 
                    max_dim = 0.001
                
                center_x = (xmax + xmin) / 2
                center_y = (ymax + ymin) / 2
                
                # Add a 15% buffer
                padded_radius = (max_dim / 2) * 1.15
                
                ex_xmin = center_x - padded_radius
                ex_xmax = center_x + padded_radius
                ey_ymin = center_y - padded_radius
                ey_ymax = center_y + padded_radius
                
                esri_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?bbox={ex_xmin},{ey_ymin},{ex_xmax},{ey_ymax}&bboxSR=4326&size=800,800&format=jpg&f=image"
                
                req = urllib.request.Request(esri_url, headers={'User-Agent': 'Mozilla/5.0'})
                try:
                    with urllib.request.urlopen(req) as response:
                        img_data = response.read()
                except Exception as img_e:
                    raise Exception(f"Failed to fetch ESRI Image. Error: {str(img_e)}")
                    
                # 2. Command Gemini to visually inspect the perfectly square tile
                gemini_res = call_gemini_vision(img_data, request.geminiKey)
                
                # 3. Clean up the response and convert image pixels to GPS coordinates
                json_str = gemini_res.strip().replace("```json", "").replace("```", "").strip()
                boxes = json.loads(json_str)
                
                for box in boxes:
                    lat_top = ey_ymax - (box.get("ymin", 0) * (ey_ymax - ey_ymin))
                    lat_bot = ey_ymax - (box.get("ymax", 0) * (ey_ymax - ey_ymin))
                    lng_left = ex_xmin + (box.get("xmin", 0) * (ex_xmax - ex_xmin))
                    lng_right = ex_xmin + (box.get("xmax", 0) * (ex_xmax - ex_xmin))
                    
                    center_lng = (lng_left + lng_right) / 2
                    center_lat = (lat_top + lat_bot) / 2
                    
                    # Deduplication
                    is_dupe = False
                    for existing in features:
                        if existing.get("properties", {}).get("source") == "Overture":
                            ec = existing["geometry"]["coordinates"][0]
                            exmin_coord, exmax_coord = min(c[0] for c in ec), max(c[0] for c in ec)
                            eymin_coord, eymax_coord = min(c[1] for c in ec), max(c[1] for c in ec)
                            if exmin_coord <= center_lng <= exmax_coord and eymin_coord <= center_lat <= eymax_coord:
                                is_dupe = True
                                break
                    if is_dupe: continue
                    
                    # Double-check that Gemini didn't find a building outside our original drawn polygon
                    if not (xmin <= center_lng <= xmax and ymin <= center_lat <= ymax):
                        continue
                    
                    geom = {
                        "type": "Polygon",
                        "coordinates": [[[lng_left, lat_top], [lng_right, lat_top], [lng_right, lat_bot], [lng_left, lat_bot], [lng_left, lat_top]]]
                    }
                    features.append({
                        "type": "Feature",
                        "geometry": geom,
                        "properties": {"source": "Gemini"}
                    })
            except Exception as e:
                print("Gemini Deep Scan Error:", str(e))

        return {"type": "FeatureCollection", "features": features}
        
    except Exception as e:
        print(f"Backend Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
