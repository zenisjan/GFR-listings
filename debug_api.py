#!/usr/bin/env python3
"""
Debug script to test the API calls and see what's actually being returned
"""

import asyncio
import httpx
from playwright.async_api import async_playwright
import json

async def debug_api():
    """Debug the API calls to see what's happening"""
    
    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            # Navigate to main page to establish session
            print("Navigating to main page...")
            await page.goto("https://drazby.fs.gov.cz/client/main", wait_until="networkidle")
            
            # Get cookies
            cookies = await context.cookies()
            print(f"Got {len(cookies)} cookies")
            
            # Create cookie header
            cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            print(f"Cookie header: {cookie_header[:100]}...")
            
            # Test the API call
            api_url = "https://drazby.fs.gov.cz/api/v02/as/data/Predmet_drazby"
            headers = {
                "Cookie": cookie_header,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://drazby.fs.gov.cz/client/main",
                "X-Requested-With": "XMLHttpRequest"
            }
            
            # Parameters for the API call (same as in main scraper)
            params = {
                'select': 'Nazev,Hlavni_miniatura.ID,ID,Akt_hod_prihozu,Mesto_prevzeti,Aukce.Mesto_prevzeti,Rozdilnost_prevzeti,Datum_od_kdy,Datum_do_kdy',
                'filter': 'ID > 0 AND Soubor = NULL AND Aukce.Zverejneno = TRUE AND datum_do_kdy > DB.GetDateTime() and Aukce.Zastaveno <> true and Aukce.Zastav_insolvence <> true',
                'orderBy': 'DESC Aukce.Datum_zverejneni'
            }
            
            print(f"Making API call to: {api_url}")
            
            async with httpx.AsyncClient() as client:
                response = await client.get(api_url, headers=headers, params=params, timeout=30.0)
                
                print(f"Response status: {response.status_code}")
                print(f"Response headers: {dict(response.headers)}")
                print(f"Response content type: {response.headers.get('content-type', 'unknown')}")
                print(f"Response text (first 500 chars): {response.text[:500]}")
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        print(f"JSON data keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                        print(f"JSON data type: {type(data)}")
                        if isinstance(data, dict) and 'data' in data:
                            print(f"Data array length: {len(data['data']) if isinstance(data['data'], list) else 'Not a list'}")
                    except json.JSONDecodeError as e:
                        print(f"JSON decode error: {e}")
                        print("Trying XML parsing...")
                        try:
                            import xml.etree.ElementTree as ET
                            root = ET.fromstring(response.text)
                            print(f"XML root tag: {root.tag}")
                            print(f"XML root children count: {len(root)}")
                            
                            # Find all auction items
                            items = root.findall('.//AjaxDictionaryOfstringanyType')
                            print(f"Found {len(items)} auction items in XML")
                            
                            if items:
                                # Show first item structure
                                first_item = items[0]
                                print(f"First item children: {[child.tag for child in first_item]}")
                                for child in first_item[:5]:  # Show first 5 fields
                                    print(f"  {child.tag}: {child.text}")
                        except ET.ParseError as xml_e:
                            print(f"XML parse error: {xml_e}")
                        print(f"Raw response (first 1000 chars): {response.text[:1000]}")
                else:
                    print(f"Non-200 response: {response.text}")
                    
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(debug_api())
