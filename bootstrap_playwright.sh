#!/bin/bash
# Bootstrap script for Playwright MCP browser installation
# Run: chmod +x bootstrap_playwright.sh && ./bootstrap_playwright.sh

set -e

echo "Installing Playwright Chrome browser for MCP server..."

# Install @playwright/test locally (avoids the npx warning)
npm install --no-save @playwright/test

# Install Chrome browser (--with-deps requires sudo for system libs)
npx playwright install --with-deps chrome

echo ""
echo "Done! Playwright Chrome is ready for the MCP server."
