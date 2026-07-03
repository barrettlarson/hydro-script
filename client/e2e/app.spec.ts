import { expect, test, type Page } from '@playwright/test'

const status = {
  devices: {
    spa_pump: { state: '0', label: 'OFF' },
    spa_heater: { state: '0', label: 'OFF' },
    spa_set_point: { state: '102', label: '102°F' },
    pool_heater: { state: '0', label: 'OFF' },
    pool_set_point: { state: '84', label: '84°F' },
    pool_pump: { state: '1', label: 'ON' },
  },
  temps: { air: 75, pool: 82, spa: null },
  setpoint_ranges: { spa: [90, 104], pool: [76, 90] },
  all_keys: [],
}

const health = {
  status: 'ok',
  is_stale: false,
  last_success_at: '2026-07-02T12:00:00+00:00',
  last_attempt_at: '2026-07-02T12:00:00+00:00',
  age_seconds: 3,
  consecutive_failures: 0,
  failures_by_category: {},
  recent_failures: [],
}

interface CapturedPost {
  path: string
  body: unknown
}

/** Mock every /api call in the browser and capture POSTs for assertions. */
async function mockApi(page: Page, posts: CapturedPost[]): Promise<void> {
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (req.method() === 'POST') {
      posts.push({ path, body: req.postData() ? JSON.parse(req.postData()!) : null })
      await route.fulfill({ json: { ok: true, action: 'test', messages: [], error: null } })
    } else if (path === '/api/status') {
      await route.fulfill({ json: status })
    } else if (path === '/api/health') {
      await route.fulfill({ json: health })
    } else {
      await route.fulfill({ status: 404, json: { detail: 'not found' } })
    }
  })
}

test.describe('read path', () => {
  test('renders temps, cards, and health', async ({ page }) => {
    await mockApi(page, [])
    await page.goto('/')
    await expect(page.getByText('75')).toBeVisible()
    await expect(page.getByText('82')).toBeVisible()
    await expect(page.getByText('Live')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Spa' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Pool' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Filter pump' })).toBeVisible()
    await expect(page.getByText('Running')).toBeVisible()
  })

  test('shows the warming-up banner while the cache is empty', async ({ page }) => {
    await page.route('**/api/status', (route) =>
      route.fulfill({ status: 503, json: { detail: 'Status is warming up.' } }),
    )
    await page.route('**/api/health', (route) =>
      route.fulfill({ json: { ...health, status: 'down', last_success_at: null } }),
    )
    await page.goto('/')
    await expect(page.getByText(/warming up/i)).toBeVisible()
    await expect(page.getByText('Offline')).toBeVisible()
  })
})

test.describe('toggles', () => {
  test('spa turn on posts to /api/spa/on', async ({ page }) => {
    const posts: CapturedPost[] = []
    await mockApi(page, posts)
    await page.goto('/')
    const spa = page.locator('section', { has: page.getByRole('heading', { name: 'Spa' }) })
    await spa.getByRole('button', { name: 'Turn on' }).click()
    await expect.poll(() => posts.map((p) => p.path)).toContain('/api/spa/on')
  })

  test('filter pump turn off posts to /api/pump/off', async ({ page }) => {
    const posts: CapturedPost[] = []
    await mockApi(page, posts)
    await page.goto('/')
    const pump = page.locator('section', {
      has: page.getByRole('heading', { name: 'Filter pump' }),
    })
    await pump.getByRole('button', { name: 'Turn off' }).click()
    await expect.poll(() => posts.map((p) => p.path)).toContain('/api/pump/off')
  })
})

test.describe('set point controls', () => {
  test('slider commits the new target once on release', async ({ page }) => {
    const posts: CapturedPost[] = []
    await mockApi(page, posts)
    await page.goto('/')
    const slider = page.getByRole('slider', { name: 'spa target temperature' })
    await expect(slider).toBeEnabled()
    await slider.focus()
    await page.keyboard.press('ArrowLeft')
    await page.keyboard.press('ArrowLeft')
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0]).toEqual({ path: '/api/spa/temp', body: { temp: 100 } })
  })

  test('stepper taps coalesce into one request', async ({ page }) => {
    const posts: CapturedPost[] = []
    await mockApi(page, posts)
    await page.goto('/')
    const plus = page.getByRole('button', { name: 'Raise pool target' })
    await expect(plus).toBeEnabled()
    await plus.click()
    await plus.click()
    await plus.click()
    await expect.poll(() => posts.length, { timeout: 5_000 }).toBe(1)
    expect(posts[0]).toEqual({ path: '/api/pool/temp', body: { temp: 87 } })
  })

  test('displayed target updates immediately while tapping', async ({ page }) => {
    const posts: CapturedPost[] = []
    await mockApi(page, posts)
    await page.goto('/')
    const spa = page.locator('section', { has: page.getByRole('heading', { name: 'Spa' }) })
    const minus = spa.getByRole('button', { name: 'Lower spa target' })
    await expect(minus).toBeEnabled()
    await minus.click()
    await expect(spa.getByText('101°')).toBeVisible()
  })
})

test.describe('auth', () => {
  test('login screen gates the app until a session exists', async ({ page }) => {
    let authed = false
    await page.route('**/api/**', async (route) => {
      const req = route.request()
      const path = new URL(req.url()).pathname
      if (req.method() === 'POST' && path === '/api/login') {
        authed = true
        await route.fulfill({ json: { ok: true } })
      } else if (!authed) {
        await route.fulfill({ status: 401, json: { detail: 'Not authenticated.' } })
      } else if (path === '/api/status') {
        await route.fulfill({ json: status })
      } else {
        await route.fulfill({ json: health })
      }
    })
    await page.goto('/')
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Spa' })).not.toBeVisible()
    await page.getByLabel('Email').fill('user@example.com')
    await page.getByLabel('Password').fill('hunter2')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Spa' })).toBeVisible()
    await expect(page.getByText('Live')).toBeVisible()
  })
})

test.describe('action failures', () => {
  test('surfaces the API error message', async ({ page }) => {
    await page.route('**/api/**', async (route) => {
      const req = route.request()
      const path = new URL(req.url()).pathname
      if (req.method() === 'POST') {
        await route.fulfill({
          status: 502,
          json: { detail: 'The pool controller or iAquaLink service is currently unavailable.' },
        })
      } else if (path === '/api/status') {
        await route.fulfill({ json: status })
      } else {
        await route.fulfill({ json: health })
      }
    })
    await page.goto('/')
    const spa = page.locator('section', { has: page.getByRole('heading', { name: 'Spa' }) })
    await spa.getByRole('button', { name: 'Turn on' }).click()
    await expect(page.getByText(/currently unavailable/)).toBeVisible()
  })
})
