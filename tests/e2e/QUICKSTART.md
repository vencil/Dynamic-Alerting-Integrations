# Playwright E2E Tests - Quick Start

Get started with smoke tests in 3 minutes.

## 1. Install Dependencies

```bash
cd tests/e2e
npm install
npx playwright install chromium
```

## 2. Run Tests

```bash
# All tests (default: headless)
npm test

# Interactive UI mode (recommended for first run)
npm run test:ui

# Watch specific test
npm run test:portal
npm run test:tenant
npm run test:group
npm run test:auth
npm run test:batch
```

## 3. View Results

```bash
# HTML report
npx playwright show-report

# Artifacts in test-results/
ls test-results/
```

## Common Workflows

### Development (add/modify tests)

```bash
# Interactive mode with time-travel debugging
npm run test:ui

# Single file
npx playwright test portal-home.spec.ts

# Specific test
npx playwright test portal-home.spec.ts -g "should load portal"

# Watch mode (re-run on file change)
npx playwright test --watch
```

### Debugging Failures

```bash
# Debug mode with DevTools
npm run test:debug

# Headed mode (see browser)
npm run test:headed

# Verbose logging
npm test -- --verbose

# Show all debug output
DEBUG=pw:api npm test
```

### CI Simulation

```bash
# Run like CI (1 worker, with retry)
npm test -- --workers=1 --retries=1
```

## File Structure

```
tests/e2e/
├── portal-home.spec.ts          # Portal loading & UI
├── tenant-manager.spec.ts       # Filtering & data display
├── group-management.spec.ts     # Group CRUD operations
├── auth-flow.spec.ts            # OAuth2 & identity
├── batch-operations.spec.ts     # Silent mode & batch ops
├── playwright.config.ts         # Playwright config
├── package.json                 # Dependencies & scripts
├── tsconfig.json                # TypeScript config
├── README.md                    # Full documentation
├── QUICKSTART.md                # This file
├── fixtures/
│   ├── mock-data.ts             # Test data fixtures
│   └── test-helpers.ts          # Helper functions
└── .gitignore
```

## Key Commands

| Command | Purpose |
|---------|---------|
| `npm test` | Run all tests headless |
| `npm run test:ui` | Interactive mode (best for dev) |
| `npm run test:headed` | Run with visible browser |
| `npm run test:debug` | Debug with DevTools |
| `npx playwright show-report` | View test results |
| `npm run test:critical` | Run @critical tag tests |

## Configuration

Edit `playwright.config.ts` to:
- Change base URL: `baseURL: 'http://custom-url:8080'`
- Add browsers: uncomment firefox/webkit in `projects`
- Adjust timeout: `timeout: 60 * 1000`

Or use environment variables:

```bash
BASE_URL=http://localhost:3000 npm test
CI=true npm test
```

## Troubleshooting

**Q: "Port 8080 in use"**
```bash
# Kill process using port 8080
lsof -ti:8080 | xargs kill -9
# Or change in playwright.config.ts webServer.url
```

**Q: "Element not found"**
```bash
# Use UI mode to inspect selectors
npm run test:ui

# Check if element exists
npx playwright test portal-home.spec.ts --debug
# Then in DevTools console: document.querySelectorAll('.tool-card')
```

**Q: "Test timeout"**
```bash
# Increase timeout in playwright.config.ts
timeout: 60 * 1000  // 60 seconds

# Or for single test
npx playwright test --timeout=60000
```

**Q: "Can't connect to localhost:8080"**
```bash
# Start portal manually
npm run serve:portal

# In another terminal
npm test
```

## Next Steps

1. Read [README.md](README.md) for full documentation
2. Check [fixtures/test-helpers.ts](fixtures/test-helpers.ts) for available utilities
3. Review [fixtures/mock-data.ts](fixtures/mock-data.ts) for test data
4. Add `@critical` tag to mark tests for smoke suite
5. Use `npm run test:ui` for interactive development

## Support

- **Playwright Docs**: https://playwright.dev
- **Project Docs**: ../../docs/
- **Test Playbook**: ../../docs/internal/testing-playbook.md
