import '@testing-library/jest-dom';

// Mock fetch
global.fetch = jest.fn();

// Reset mocks before each test
beforeEach(() => {
  (global.fetch as jest.Mock).mockClear();
});
