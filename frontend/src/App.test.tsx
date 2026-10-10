import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import App from './App'

describe('App', () => {
  afterEach(() => window.history.replaceState(null, '', '/'))

  it('最初は要求の入力画面を表示する', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: 'どんな指標を見たいですか？' })).toBeInTheDocument()
    expect(screen.getByLabelText('見たい指標')).toBeInTheDocument()
  })

  it('デモでは BEAM Canvas と Event Matrix を表示する', () => {
    window.history.replaceState(null, '', '/?demo')
    render(<App />)
    expect(screen.getByText('顧客が商品を注文する', { selector: 'h3' })).toBeInTheDocument()
    expect(screen.getByTestId('w-who')).toHaveTextContent('会員ランク')
    expect(screen.getByTestId('w-who')).toHaveTextContent('年収（データなし）')
    expect(screen.getByTestId('how-many')).toHaveTextContent('売上金額')
    expect(screen.getByLabelText('顧客が商品を注文する × 店舗')).toHaveTextContent('✓')
    expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeDisabled()
  })
})
