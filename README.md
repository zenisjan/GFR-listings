# Czech Government Auction Scraper

An Apify Actor for scraping auction listings from the Czech government auction website (drazby.fs.gov.cz).

## Features

- **Comprehensive Scraping**: Scrapes auction listings across multiple auction types
- **Database Integration**: PostgreSQL database support with full schema
- **Browser Automation**: Optional Playwright support for JavaScript-heavy pages
- **Detailed Data Extraction**: Extracts comprehensive auction information
- **Pagination Support**: Handles pagination automatically
- **Error Handling**: Robust error handling and retry logic
- **Rate Limiting**: Built-in rate limiting to respect the website

## Auction Types

The scraper supports the following auction types:
- Real Estate
- Vehicles
- Machinery & Equipment
- Jewelry & Precious Metals
- Art & Collectibles
- Electronics
- Furniture & Household
- Other

## Input Configuration

### Required Parameters
- `auctionTypes`: Array of auction types to scrape

### Optional Parameters
- `maxListings`: Maximum number of listings per type (0 = unlimited)
- `includeDetailedData`: Whether to scrape detailed information from individual auction pages
- `searchQuery`: Optional search term to filter auctions
- `location`: Optional location filter (region or city)
- `priceMin`: Minimum starting price filter in CZK
- `priceMax`: Maximum starting price filter in CZK
- `auctionStatus`: Filter auctions by status (all, active, upcoming, ended)
- `useBrowser`: Use browser automation for JavaScript-heavy pages

## Database Schema

The scraper uses a PostgreSQL database with the following main tables:

### `actor_runs`
Stores information about each scraping run.

### `government_auctions`
Stores the scraped auction data with fields including:
- Basic information (id, title, url, auction_type)
- Pricing (starting_price, current_price)
- Location and contact details
- Auction dates and status
- Property details (for real estate)
- Legal information
- Images and similar auctions

## Setup

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Database Setup**:
   ```sql
   -- Run the setup_database.sql script to create tables
   psql -d your_database -f setup_database.sql
   ```

3. **Environment Variables**:
   Set the following environment variables for database connection:
   - `DB_HOST`: Database host
   - `DB_PORT`: Database port (default: 5432)
   - `DB_NAME`: Database name
   - `DB_USER`: Database username
   - `DB_PASSWORD`: Database password
   - `DB_SSL_MODE`: SSL mode (default: prefer)
   - `DB_POOL_SIZE`: Connection pool size (default: 5)

## Usage

### Local Development
```bash
python src/main.py
```

### Apify Platform
1. Push the actor to Apify:
   ```bash
   apify push
   ```

2. Run with input:
   ```bash
   apify run --input='{"auctionTypes": ["real_estate", "vehicles"], "maxListings": 50}'
   ```

## Example Input

```json
{
  "auctionTypes": ["real_estate", "vehicles"],
  "maxListings": 100,
  "includeDetailedData": true,
  "searchQuery": "apartment",
  "location": "Prague",
  "priceMin": 1000000,
  "priceMax": 5000000,
  "auctionStatus": "active",
  "useBrowser": true
}
```

## Output

The scraper outputs auction data in the following format:

```json
{
  "id": "auction_12345",
  "title": "Apartment in Prague",
  "url": "https://drazby.fs.gov.cz/client/auction/12345",
  "auction_type": "real_estate",
  "starting_price": 2500000,
  "starting_price_text": "2 500 000 Kč",
  "current_price": 2750000,
  "current_price_text": "2 750 000 Kč",
  "description": "Beautiful apartment in Prague center",
  "full_description": "Detailed description...",
  "location": "Prague 1",
  "region": "Prague",
  "auction_date": "2024-01-15T10:00:00Z",
  "auction_end_date": "2024-01-15T12:00:00Z",
  "registration_deadline": "2024-01-14T18:00:00Z",
  "auction_status": "active",
  "views": 150,
  "bids_count": 5,
  "is_featured": false,
  "image_url": "https://example.com/image.jpg",
  "images": ["https://example.com/image1.jpg", "https://example.com/image2.jpg"],
  "contact_name": "John Doe",
  "contact_phone": "+420 123 456 789",
  "contact_email": "john@example.com",
  "coordinates_lat": 50.0755,
  "coordinates_lng": 14.4378,
  "property_details": {
    "area": "85 m²",
    "rooms": "3+1"
  },
  "legal_details": "Legal information...",
  "similar_auctions": [
    {
      "title": "Similar apartment",
      "url": "https://example.com/similar"
    }
  ],
  "scraped_at": "2024-01-10T15:30:00Z"
}
```

## Database Views

The scraper creates several useful database views:

- `latest_auctions`: Latest version of each auction
- `actor_run_stats`: Statistics for each scraping run
- `auction_type_stats`: Statistics by auction type

## Error Handling

The scraper includes comprehensive error handling:
- Database connection retry logic
- HTTP request retry with exponential backoff
- Graceful handling of missing data
- Connection pool management for long-running operations

## Rate Limiting

The scraper includes built-in rate limiting:
- 2-second delay between pages
- 1-second delay for detailed scraping
- Respectful scraping practices

## Browser Automation

For JavaScript-heavy pages, the scraper can use Playwright:
- Automatic browser initialization
- Network idle waiting
- Proper browser cleanup
- Fallback to HTTP requests if browser fails

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

MIT License - see LICENSE file for details.
