import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// vitest のグローバルを使っていないので、描画の後片付けを明示する
afterEach(() => cleanup())
