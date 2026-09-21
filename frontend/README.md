# Simulation Dashboard Frontend

A React + TypeScript + TailwindCSS dashboard for monitoring and controlling the Agentic Memory Engine simulation system (spec § 3.7 + § 4).

## Features

- **Simulation Control Panel**: Start/Stop/Pause buttons, duration selector (1/2/4/6 minutes), real-time progress bar
- **User State Panel**: 5 tabs (user_1 to user_5) with current facts, contradictions, memory counts, and profile cache status
- **Audit Log Viewer**: Real-time event stream with filtering by user, event type, and fact ID
- **Validation Dashboard**: Live test case results with cross-validation status and pass/fail summary
- **Metrics Display**: Cache hit rate, latency, API calls, facts counts, and storage usage

## Architecture

- **Vite** for fast development and build
- **React 18** for UI components
- **TypeScript** for type safety
- **TailwindCSS** for styling with dark/light mode support
- **Custom hooks** for polling data at 500ms intervals
- **API client** with fetch wrapper and error handling

## Setup

### Prerequisites

- Node.js 18+
- npm or yarn

### Installation

```bash
cd frontend
npm install
```

### Development

Start the development server:

```bash
npm run dev
```

The dashboard will be available at `http://localhost:3000`.

**Note:** The backend Dashboard API must be running on `http://localhost:8000` for the frontend to work.

### Build

```bash
npm run build
```

This creates an optimized production build in `dist/`.

### Testing

Run the test suite:

```bash
npm test
```

Run tests in watch mode:

```bash
npm run test:watch
```

## API Integration

The frontend communicates with the Dashboard API at `http://localhost:8000`:

- **POST /simulate/start** - Start a new simulation run
- **GET /simulate/{run_id}/status** - Poll run status and progress (every 500ms)
- **POST /simulate/{run_id}/stop** - Stop a running simulation
- **GET /users/{user_id}/facts** - Get facts for a user
- **GET /users/{user_id}/memory-count** - Get active/inactive fact counts
- **GET /users/{user_id}/profile-cache** - Get profile cache status
- **GET /audit/events** - Query audit log events with filters
- **GET /validation/results** - Get validation test case results
- **GET /validation/summary** - Get validation summary statistics
- **GET /metrics** - Get aggregated metrics

## Project Structure

```
frontend/
├── src/
│   ├── __tests__/
│   │   ├── api.test.ts              # API client tests
│   │   ├── components.test.tsx      # Component tests
│   │   └── integration.test.tsx     # Integration tests
│   ├── api/
│   │   └── client.ts                # API client with fetch wrapper
│   ├── components/
│   │   ├── SimulationControl.tsx   # Control panel component
│   │   ├── UserStatePanel.tsx      # User state tabs
│   │   ├── AuditLogViewer.tsx      # Audit log viewer
│   │   ├── ValidationDashboard.tsx # Validation results
│   │   └── MetricsDisplay.tsx      # Metrics tiles
│   ├── hooks/
│   │   └── usePolling.ts           # Custom polling hook
│   ├── types.ts                     # TypeScript type definitions
│   ├── App.tsx                      # Main app layout
│   ├── main.tsx                     # React entry point
│   ├── index.css                    # Tailwind CSS
│   └── setupTests.ts                # Jest setup
├── index.html                        # HTML entry point
├── package.json
├── vite.config.ts
├── tailwind.config.ts
├── postcss.config.ts
├── jest.config.ts
└── tsconfig.json
```

## Key Components

### SimulationControl.tsx
- Buttons: Start, Stop
- Duration selector: 1/2/4/6 minute radio buttons
- Progress bar with elapsed/remaining time display
- Real-time status indicator

### UserStatePanel.tsx
- 5 tabs for users (user_1 to user_5)
- Active/inactive memory count pills
- Current facts list with timestamps
- Recent contradictions with arrows (old → new)
- Profile cache status (Hit/Miss/TTL)

### AuditLogViewer.tsx
- Real-time event stream (newest first, auto-scrolling)
- Collapsible filters for user, event type, fact ID
- Event details with before/after state transitions
- Pagination support (limit=100)

### ValidationDashboard.tsx
- Summary row: passed/failed counts and pass rate %
- Cross-validation status indicator (all methods agree Y/N)
- Results table with scenario ID, status, and details
- Green rows for passes, red rows for failures

### MetricsDisplay.tsx
- Stat tiles: cache hit rate %, latency (ms), API calls
- Fact counts: created/updated/deactivated/expired
- Storage usage: memory.db and audit_log.db sizes
- Auto-refresh every 500ms

## Dark Mode

The dashboard supports dark/light mode toggling via the moon/sun icon in the header. The preference is stored in localStorage.

## Polling Strategy

All data sources are polled at 500ms intervals:
- Simulation status (when run is active)
- User facts, memory counts, profile cache
- Audit events
- Validation results
- Metrics

The custom `usePolling` hook handles all polling logic with automatic cleanup and error handling.

## Testing

### 30+ Component Tests

- **api.test.ts**: 20+ tests for API client (start, status, facts, events, validation, metrics)
- **components.test.tsx**: 30+ tests for React components (render, interaction, filtering, polling)
- **integration.test.tsx**: 5+ tests for complete flows (start → poll → complete → validation)

### Mock Fetch

All tests mock `window.fetch` with Jest and test API responses, error handling, and edge cases.

## Browser Support

- Chrome/Edge 90+
- Firefox 88+
- Safari 14+
- Responsive design for 1024px+ width (desktop focus)

## Performance

- Fast dev server with Vite (< 100ms HMR)
- Optimized production build with tree-shaking
- TailwindCSS purging unused styles
- Lazy polling (disabled when component unmounted)
- Efficient re-renders via React hooks

## Future Enhancements

- WebSocket support for real-time updates (instead of polling)
- Export audit log to CSV
- Custom date range pickers for event filtering
- Real-time chart visualization of metrics trends
- User preferences/settings panel
- Mobile responsive improvements

## Troubleshooting

### Frontend not connecting to backend?
- Ensure Dashboard API is running on `http://localhost:8000`
- Check CORS is enabled on the backend
- Verify no firewall is blocking localhost:8000

### Dark mode not working?
- Clear browser localStorage if stuck in wrong mode
- Check CSS is loading (Tailwind classes applied)

### Tests failing?
- Clear node_modules and reinstall: `rm -rf node_modules && npm install`
- Clear Jest cache: `npm test -- --clearCache`

## Contributing

When adding new components:
1. Create component in `src/components/`
2. Add corresponding tests in `src/__tests__/`
3. Update types in `src/types.ts` if needed
4. Update API client in `src/api/client.ts`

## License

Part of the Agentic Memory Engine project.
