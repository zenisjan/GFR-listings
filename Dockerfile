# Optimized Docker image with minimal Playwright usage
FROM apify/actor-python:3.13

# Copy requirements.txt first for better Docker layer caching
COPY requirements.txt ./

# Install Python dependencies
RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

# Install Playwright system dependencies and browsers (minimal)
RUN playwright install-deps chromium
RUN playwright install chromium

# Copy source code
COPY . ./

# Compile Python code for faster startup
RUN python3 -m compileall -q src/

# Run the optimized scraper
CMD ["python3", "-m", "src"]