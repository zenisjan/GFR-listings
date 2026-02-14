-- Database setup for GFR scraper
-- This schema matches the existing database structure with listings table

-- Create the actor_runs table first (parent table)
CREATE TABLE IF NOT EXISTS actor_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(100) UNIQUE NOT NULL,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP WITH TIME ZONE,
    categories TEXT[],
    max_listings INTEGER,
    search_query TEXT,
    location_filter TEXT,
    price_min INTEGER,
    price_max INTEGER,
    total_listings_scraped INTEGER DEFAULT 0,
    status VARCHAR(50) DEFAULT 'running',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create the main listings table
CREATE TABLE IF NOT EXISTS listings (
    id VARCHAR(50) NOT NULL,
    actor_run_id INTEGER NOT NULL REFERENCES actor_runs(id) ON DELETE CASCADE,
    scraper_name VARCHAR(100) NOT NULL DEFAULT 'gfr',
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    category VARCHAR(50) NOT NULL,
    price INTEGER,
    price_text VARCHAR(100),
    description TEXT,
    full_description TEXT,
    location TEXT,
    views INTEGER DEFAULT 0,
    date VARCHAR(100),
    is_top BOOLEAN DEFAULT FALSE,
    image_url TEXT,
    contact_name VARCHAR(255),
    phone VARCHAR(100),
    coordinates_lat DECIMAL(10, 8),
    coordinates_lng DECIMAL(11, 8),
    images JSONB,
    similar_listings JSONB,
    scraped_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Primary key using listing ID, actor run ID, and scraper name
    PRIMARY KEY (id, actor_run_id, scraper_name)
);

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_actor_runs_run_id ON actor_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_actor_runs_start_time ON actor_runs(start_time);
CREATE INDEX IF NOT EXISTS idx_actor_runs_status ON actor_runs(status);

CREATE INDEX IF NOT EXISTS idx_listings_actor_run_id ON listings(actor_run_id);
CREATE INDEX IF NOT EXISTS idx_listings_scraper_name ON listings(scraper_name);
CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_location ON listings(location);
CREATE INDEX IF NOT EXISTS idx_listings_scraped_at ON listings(scraped_at);
CREATE INDEX IF NOT EXISTS idx_listings_is_top ON listings(is_top);
CREATE INDEX IF NOT EXISTS idx_listings_scraper_category ON listings(scraper_name, category);

-- Create a view for easy querying of latest listings
CREATE OR REPLACE VIEW latest_listings AS
SELECT DISTINCT ON (l.id, l.scraper_name) 
    l.id,
    l.scraper_name,
    l.title,
    l.url,
    l.category,
    l.price,
    l.price_text,
    l.description,
    l.full_description,
    l.location,
    l.views,
    l.date,
    l.is_top,
    l.image_url,
    l.contact_name,
    l.phone,
    l.coordinates_lat,
    l.coordinates_lng,
    l.images,
    l.similar_listings,
    l.scraped_at,
    ar.run_id,
    ar.start_time as actor_run_start
FROM listings l
JOIN actor_runs ar ON l.actor_run_id = ar.id
ORDER BY l.id, l.scraper_name, l.scraped_at DESC;

-- Create a view for actor run statistics
CREATE OR REPLACE VIEW actor_run_stats AS
SELECT 
    ar.id,
    ar.run_id,
    ar.start_time,
    ar.end_time,
    ar.categories,
    ar.max_listings,
    ar.search_query,
    ar.location_filter,
    ar.price_min,
    ar.price_max,
    ar.total_listings_scraped,
    ar.status,
    ar.created_at,
    COUNT(l.id) as actual_listings_count,
    COUNT(DISTINCT l.category) as categories_scraped,
    COUNT(DISTINCT l.scraper_name) as scrapers_used,
    MIN(l.scraped_at) as first_listing_scraped,
    MAX(l.scraped_at) as last_listing_scraped
FROM actor_runs ar
LEFT JOIN listings l ON ar.id = l.actor_run_id
GROUP BY ar.id, ar.run_id, ar.start_time, ar.end_time, ar.categories, 
         ar.max_listings, ar.search_query, ar.location_filter, ar.price_min, 
         ar.price_max, ar.total_listings_scraped, ar.status, ar.created_at;

-- Create a view for scraper-specific statistics
CREATE OR REPLACE VIEW scraper_stats AS
SELECT 
    l.scraper_name,
    COUNT(*) as total_listings,
    COUNT(DISTINCT l.category) as categories_scraped,
    COUNT(DISTINCT l.actor_run_id) as total_runs,
    MIN(l.scraped_at) as first_scraped,
    MAX(l.scraped_at) as last_scraped,
    AVG(l.price) as avg_price,
    COUNT(CASE WHEN l.is_top THEN 1 END) as top_listings_count
FROM listings l
GROUP BY l.scraper_name
ORDER BY total_listings DESC;

-- Grant permissions (adjust as needed for your setup)
-- GRANT ALL PRIVILEGES ON TABLE actor_runs TO your_username;
-- GRANT ALL PRIVILEGES ON TABLE listings TO your_username;
-- GRANT ALL PRIVILEGES ON VIEW latest_listings TO your_username;
-- GRANT ALL PRIVILEGES ON VIEW actor_run_stats TO your_username;
-- GRANT ALL PRIVILEGES ON VIEW scraper_stats TO your_username;
-- GRANT USAGE, SELECT ON SEQUENCE actor_runs_id_seq TO your_username;
