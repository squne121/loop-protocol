// Fixture for AC6: deterministic Vite-specific resolution.
import './styles/global.css'
import logoUrl from './logo.png?url'
import dataRaw from './data.txt?raw'

const iconUrl = new URL('./media/icon.svg', import.meta.url)

export { logoUrl, dataRaw, iconUrl }
