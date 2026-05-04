#!/bin/bash
# Bootstrap script for Playwright MCP browser installation
# Run: chmod +x bootstrap_playwright.sh && ./bootstrap_playwright.sh

set -e

echo "Installing Playwright Chromium browser for MCP server..."

# Install @playwright/test locally (avoids the npx warning)
npm install --no-save @playwright/test

# Install bundled Chromium (no sudo needed, no system changes)
npx playwright install chromium

# Install Chromium for @playwright/mcp (may use a different version)
npx @playwright/mcp install-browser chromium

echo ""
echo "Done! Playwright Chromium is ready for the MCP server."
