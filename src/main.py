#!/usr/bin/env python3
"""
Optimized Czech Government Auction Scraper
==========================================

This scraper directly calls the API endpoint to get all auction data at once,
eliminating the need for browser automation and complex pagination logic.

The API provides comprehensive auction data including:
- All auction details (title, description, price, location, dates)
- Contact information
- Images
- Auction status and metadata

This optimized version:
- Uses only HTTP requests (no Playwright)
- Single API call gets all data
- Applies filters after fetching data
- Much faster and more reliable
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from apify import Actor
from bs4 import BeautifulSoup

try:
    from .czech_cities import geocode_czech_city
except ImportError:
    from czech_cities import geocode_czech_city


# Bounded exponential backoff for transient fetch failures (timeouts, resets,
# 429/5xx). A single flaky response used to abort an entire category scrape.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _request_with_retry(client, method, url, attempts=3, **kwargs):
    """Issue a request, retrying transient failures with exponential backoff.

    Returns the final response without raising for status (callers keep their
    own raise_for_status()); re-raises the last transport error if the
    connection itself keeps failing.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            if attempt == attempts:
                raise
            reason = repr(e)
        else:
            if response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                return response
            reason = f"HTTP {response.status_code}"
        delay = 2.0 * (2 ** (attempt - 1))
        Actor.log.warning(
            f"Transient fetch failure ({reason}); retry {attempt}/{attempts - 1} "
            f"in {delay:.0f}s: {url}"
        )
        await asyncio.sleep(delay)


class _FailRunOnCrash:
    """Marks the actor_runs row 'failed' if the run dies with an unhandled error.

    Historically a terminal status was written only on the happy path, so a crash
    (or an Apify hard-timeout kill) left the row 'running' forever and the web app
    could not tell a dead run from a live one — sold-detection then mistreated the
    run's listings. Used as `async with Actor, _FailRunOnCrash():` so it exits
    before Actor; the exception still propagates and fails the platform run too.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                # db_manager is only imported inside main() in this scraper, so
                # resolve it lazily here as well.
                try:
                    from .database import db_manager
                except ImportError:
                    from database import db_manager
                Actor.log.error(f"Run failed with unhandled error: {exc!r}")
                db_manager.update_actor_run_status("failed", 0)
                db_manager.close_pool()
            except Exception as finalize_error:
                Actor.log.error(f"Could not mark run failed: {finalize_error}")
        return False


class OptimizedGovernmentAuctionScraper:
    """Optimized scraper that uses browser for session establishment and HTTP for API calls."""
    
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.api_url = "https://drazby.fs.gov.cz/api/v02/as/data/Predmet_drazby"
        
    async def scrape_all_auctions(
        self,
        max_listings: int = 0,
        auction_status: str = "active",
        price_min: int = 0,
        price_max: int = 0,
        location: Optional[str] = None,
        search_query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Scrape all auctions from the API and apply filters.
        
        Args:
            max_listings: Maximum number of listings to return (0 = all)
            auction_status: Filter by status ("all", "active", "ended")
            price_min: Minimum price filter in CZK
            price_max: Maximum price filter in CZK
            location: Location filter (city/region)
            search_query: Search term to filter titles/descriptions
            
        Returns:
            List of auction dictionaries
        """
        try:
            # Step 1: Establish session via auth API
            Actor.log.info("Establishing session with the API...")
            session_ok = await self._establish_session()

            if not session_ok:
                Actor.log.error("Failed to establish session")
                return []

            # Step 2: Call API to get all auction data
            Actor.log.info("Fetching all auction data from API...")
            all_auctions = await self._fetch_all_auctions()
            
            if not all_auctions:
                Actor.log.warning("⚠️ No auction data received from API")
                return []
            
            Actor.log.info(f"✅ Successfully fetched {len(all_auctions)} total auctions from API")
            
            # Step 3: Apply filters
            Actor.log.info("🔍 Applying filters to auction data...")
            filtered_auctions = self._apply_filters(
                all_auctions, auction_status, price_min, price_max, location, search_query
            )
            
            Actor.log.info(f"📊 After filtering: {len(filtered_auctions)} auctions match criteria")
            
            # Step 4: Limit results if requested
            if max_listings > 0 and len(filtered_auctions) > max_listings:
                filtered_auctions = filtered_auctions[:max_listings]
                Actor.log.info(f"✂️ Limited to {max_listings} auctions as requested")
            
            # Step 5: Convert to standardized format
            Actor.log.info("🔄 Converting to standardized format...")
            standardized_auctions = []
            for auction_data in filtered_auctions:
                standardized = self._convert_to_standard_format(auction_data)
                if standardized:
                    standardized_auctions.append(standardized)
            
            Actor.log.info(f"🎯 Final result: {len(standardized_auctions)} auctions ready for storage")
            return standardized_auctions
            
        except Exception as e:
            Actor.log.error(f"❌ Error in scrape_all_auctions: {e}")
            return []
    
    async def _establish_session(self) -> bool:
        """Establish a session via the site's auth API (no browser needed)."""
        try:
            # Step 1: Auth check — creates a server-side session
            resp = await self.client.post(
                "https://drazby.fs.gov.cz/api/v01/as/auth/check",
                params={"winAuth": "true", "sso": "true"},
            )
            Actor.log.info(f"Auth check: {resp.status_code}")

            # Step 2: Login as public/anonymous user
            resp = await self.client.get(
                "https://drazby.fs.gov.cz/api/v01/as/owmanager/Login",
                params={"language": "cs-cz"},
            )
            Actor.log.info(f"Login: {resp.status_code}")

            return resp.status_code == 200
        except Exception as e:
            Actor.log.error(f"Failed to establish session: {e}")
            return False
    
    async def _fetch_all_auctions(self) -> List[Dict[str, Any]]:
        """Fetch all auction data from the API using the same query the SPA uses."""
        try:
            # Use the same filter the SPA sends — double-URL-encoded as the server expects
            # Use exactly the fields the SPA requests — no extras
            params = {
                "select": ("Nazev,Hlavni_miniatura.ID,ID,Akt_hod_prihozu,"
                           "Mesto_prevzeti,Aukce.Mesto_prevzeti,Rozdilnost_prevzeti,"
                           "Datum_od_kdy,Datum_do_kdy"),
                "filter": ("ID%20%3E%200%20AND%20Soubor%20%3D%20NULL%20AND%20"
                           "Aukce.Zverejneno%20%3D%20TRUE%20AND%20"
                           "datum_do_kdy%20%3E%20DB.GetDateTime()%20and%20"
                           "Aukce.Zastaveno%20%3C%3E%20true%20and%20"
                           "Aukce.Zastav_insolvence%20%3C%3E%20true"),
                "orderBy": "DESC Aukce.Datum_zverejneni",
            }
            response = await _request_with_retry(self.client, "GET", self.api_url, params=params, timeout=60.0)

            if response.status_code not in (200, 206):
                Actor.log.error(f"API returned {response.status_code}: {response.text[:500]}")
                return []

            data = response.json()

            if isinstance(data, list):
                Actor.log.info(f"API returned {len(data)} auction records")
                return data
            else:
                Actor.log.warning(f"Unexpected API response format: {type(data)}")
                return []

        except Exception as e:
            Actor.log.error(f"Failed to fetch auctions from API: {e}")
            return []
    
    def _apply_filters(
        self,
        auctions: List[Dict[str, Any]],
        auction_status: str,
        price_min: int,
        price_max: int,
        location: Optional[str],
        search_query: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Apply filters to the auction data."""
        filtered = auctions.copy()
        
        # Filter by auction status
        if auction_status == "active":
            filtered = [a for a in filtered if not a.get('Priklepnuto', True)]
            Actor.log.info(f"🟢 Filtered to active auctions: {len(filtered)} remaining")
        elif auction_status == "ended":
            filtered = [a for a in filtered if a.get('Priklepnuto', False)]
            Actor.log.info(f"🔴 Filtered to ended auctions: {len(filtered)} remaining")
        # "all" means no status filtering
        
        # Filter by price range
        if price_min > 0 or price_max > 0:
            price_filtered = []
            for auction in filtered:
                price = auction.get('Akt_hod_prihozu', 0)
                if isinstance(price, (int, float)):
                    if price_min > 0 and price < price_min:
                        continue
                    if price_max > 0 and price > price_max:
                        continue
                    price_filtered.append(auction)
            filtered = price_filtered
            Actor.log.info(f"💰 Price filtered: {len(filtered)} remaining")
        
        # Filter by location
        if location and location.strip():
            location_lower = location.lower().strip()
            location_filtered = []
            for auction in filtered:
                auction_location = auction.get('Mesto_prevzeti', '').lower()
                if location_lower in auction_location:
                    location_filtered.append(auction)
            filtered = location_filtered
            Actor.log.info(f"📍 Location filtered: {len(filtered)} remaining")
        
        # Filter by search query
        if search_query and search_query.strip():
            query_lower = search_query.lower().strip()
            search_filtered = []
            for auction in filtered:
                title = auction.get('Nazev', '').lower()
                description = auction.get('Popis', '').lower()
                if query_lower in title or query_lower in description:
                    search_filtered.append(auction)
            filtered = search_filtered
            Actor.log.info(f"🔍 Search filtered: {len(filtered)} remaining")
        
        return filtered
    
    def _geocode_location(self, location: str) -> Optional[float]:
        """Return latitude for a Czech city name, or None."""
        if not location:
            return None
        coords = geocode_czech_city(location.strip())
        return coords[0] if coords else None

    def _geocode_location_lng(self, location: str) -> Optional[float]:
        """Return longitude for a Czech city name, or None."""
        if not location:
            return None
        coords = geocode_czech_city(location.strip())
        return coords[1] if coords else None

    def _convert_to_standard_format(self, api_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert API data to standardized listing format."""
        try:
            # Extract auction ID
            auction_id = str(api_data.get('ID', ''))
            if not auction_id:
                return None
            
            # Build auction URL
            auction_url = f"https://drazby.fs.gov.cz/client/main?formName=predmet&selectedID={auction_id}"
            
            # Extract basic information (only fields available from API query)
            title = api_data.get('Nazev', '')
            price = api_data.get('Akt_hod_prihozu', 0)
            # Location: use Mesto_prevzeti, fall back to Aukce.Mesto_prevzeti
            location = api_data.get('Mesto_prevzeti', '') or api_data.get('Aukce.Mesto_prevzeti', '')

            # Extract dates
            date_from = api_data.get('Datum_od_kdy', '')
            date_to = api_data.get('Datum_do_kdy', '')

            # Image URL: use the Predmet_drazby file endpoint with the auction ID
            image_url = f"https://drazby.fs.gov.cz/api/v02/as/data/Predmet_drazby/{auction_id}/Hlavni_miniatura/Data"

            # Build description from available data
            full_description = title
            if date_from:
                full_description += f"\n\nZačátek dražby: {date_from}"
            if date_to:
                full_description += f"\n\nKonec dražby: {date_to}"
            
            # Build images array
            images = []
            if image_url:
                images.append(image_url)
            
            # All results from the active-auctions filter are active
            is_knocked_down = False

            return {
                'id': auction_id,
                'title': title,
                'url': auction_url,
                'category': 'government_auction',
                'price': float(price) if isinstance(price, (int, float)) else 0.0,
                'price_text': f"{price:,.0f} Kč" if price > 0 else '',
                'description': title[:500] if title else '',
                'full_description': full_description,
                'location': location,
                'views': 0,
                'date': date_from,
                'is_top': False,
                'image_url': image_url,
                'contact_name': '',
                'phone': '',
                'coordinates_lat': self._geocode_location(location),
                'coordinates_lng': self._geocode_location_lng(location),
                'images': json.dumps(images),
                'similar_listings': json.dumps([]),
                'scraped_at': datetime.now().isoformat(),
                'is_knocked_down': is_knocked_down,
                'date_from': date_from,
                'date_to': date_to,
            }
            
        except Exception as e:
            Actor.log.warning(f"⚠️ Error converting auction data: {e}")
            return None


async def main():
    """Main function to run the optimized scraper."""
    # Initialize the Actor
    async with Actor, _FailRunOnCrash():
        Actor.log.info("🚀 Starting Optimized Czech Government Auction Scraper")
        Actor.log.info("=" * 60)
        
        # Get input parameters
        input_data = await Actor.get_input() or {}
        
        max_listings = input_data.get('maxListings', 0)
        auction_status = input_data.get('auctionStatus', 'active')
        price_min = input_data.get('priceMin', 0)
        price_max = input_data.get('priceMax', 0)
        location = input_data.get('location', '')
        search_query = input_data.get('searchQuery', '')
        
        Actor.log.info(f"📋 Configuration:")
        Actor.log.info(f"   Max listings: {max_listings if max_listings > 0 else 'unlimited'}")
        Actor.log.info(f"   Auction status: {auction_status}")
        Actor.log.info(f"   Price range: {price_min}-{price_max if price_max > 0 else 'unlimited'} CZK")
        Actor.log.info(f"   Location filter: {location if location else 'none'}")
        Actor.log.info(f"   Search query: {search_query if search_query else 'none'}")
        
        # Initialize database connection
        db_manager_available = False
        try:
            from .database import db_manager

            # Get scraper name from environment or use default
            scraper_name = os.environ.get('SCRAPER_NAME', 'gfr')
            db_manager.scraper_name = scraper_name

            db_manager.initialize_pool()

            # Create actor run record
            actor_run_id = os.environ.get('APIFY_ACTOR_RUN_ID') or os.environ.get('ACTOR_RUN_ID', 'local-run')
            actor_run_start = datetime.now()
            db_manager.set_actor_run_info(actor_run_id, actor_run_start)
            
            db_manager.create_actor_run(
                categories=['government_auction'],  # Single category since API provides all
                max_listings=max_listings,
                search_query=search_query,
                location=location,
                price_min=price_min,
                price_max=price_max
            )
            
            Actor.log.info("✅ Database connection established and actor run created")
            db_manager_available = True
            
        except Exception as e:
            Actor.log.error(f"❌ Failed to initialize database: {e}")
            Actor.log.warning("⚠️ Continuing without database integration - data will be stored in Apify dataset only")
            db_manager_available = False
        
        # Create HTTP client with proper headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'cs,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            scraper = OptimizedGovernmentAuctionScraper(client)

            # Scrape all auctions (no browser needed — uses HTTP auth flow)
            all_auctions = await scraper.scrape_all_auctions(
                max_listings=max_listings,
                auction_status=auction_status,
                price_min=price_min,
                price_max=price_max,
                location=location,
                search_query=search_query
            )
            
            # Save data to both Apify dataset and database
            if all_auctions:
                # Save to Apify dataset (all auctions)
                await Actor.push_data(all_auctions)
                Actor.log.info(f"💾 Saved {len(all_auctions)} auctions to Apify dataset")
                
                # Save to database if available (only active auctions)
                if db_manager_available:
                    try:
                        # Filter for database: only include active auctions (is_knocked_down: false)
                        db_auctions = [auction for auction in all_auctions if not auction.get('is_knocked_down', True)]
                        Actor.log.info(f"🔍 Filtering for database: {len(db_auctions)} active auctions out of {len(all_auctions)} total")
                        
                        if db_auctions:
                            Actor.log.info("💾 Saving active auctions to database...")
                            db_manager.insert_listings(db_auctions)
                            Actor.log.info(f"✅ Saved {len(db_auctions)} active listings to database")
                        else:
                            Actor.log.info("ℹ️ No active auctions to save to database")
                    except Exception as e:
                        Actor.log.error(f"❌ Failed to save listings to database: {e}")
                        # Try to refresh the connection pool and retry once
                        try:
                            Actor.log.info("🔄 Attempting to refresh connection pool and retry database operation")
                            db_manager.refresh_pool()
                            db_auctions = [auction for auction in all_auctions if not auction.get('is_knocked_down', True)]
                            if db_auctions:
                                db_manager.insert_listings(db_auctions)
                                Actor.log.info(f"✅ Successfully saved {len(db_auctions)} active listings to database after retry")
                        except Exception as retry_e:
                            Actor.log.error(f"❌ Failed to save listings to database even after retry: {retry_e}")
            else:
                Actor.log.warning("⚠️ No auctions found matching the specified criteria")
            
            # Zero auctions almost always means upstream breakage (API change,
            # auth/session failure) rather than an empty market — mark the run
            # 'failed' so sold-detection ignores it instead of flagging every
            # previously-seen listing as sold.
            run_status = 'completed' if all_auctions else 'failed'

            # Update actor run status in database
            if db_manager_available:
                try:
                    db_manager.update_actor_run_status(run_status, len(all_auctions))
                    Actor.log.info("✅ Updated actor run status in database")
                except Exception as e:
                    Actor.log.error(f"❌ Failed to update actor run status: {e}")
            
            # Final summary
            Actor.log.info("=" * 60)
            Actor.log.info("🎉 SCRAPING COMPLETED SUCCESSFULLY")
            Actor.log.info(f"📊 Total auctions scraped: {len(all_auctions)}")
            if all_auctions and db_manager_available:
                db_auctions_count = len([auction for auction in all_auctions if not auction.get('is_knocked_down', True)])
                Actor.log.info(f"💾 Data saved to: Apify dataset ({len(all_auctions)} auctions) and database ({db_auctions_count} active auctions)")
            else:
                Actor.log.info(f"💾 Data saved to: {'Apify dataset' + (' and database' if db_manager_available else '')}")
            Actor.log.info("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
