// Procedural pixel-art sprite atlas for the device world.
// Everything is drawn at runtime into offscreen canvases — no binary assets.

export type StatusKind = 'ready' | 'partial' | 'offline'

export const STATUS_COLOR: Record<StatusKind, string> = {
  ready: '#77f2b4',
  partial: '#f7c66f',
  offline: '#ff6b7a',
}

export function hashString(text: string): number {
  let hash = 2166136261
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

function mulberry(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let value = Math.imul(state ^ (state >>> 15), 1 | state)
    value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}

function makeCanvas(width: number, height: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  return [canvas, canvas.getContext('2d')!]
}

/** Paint a sprite from string rows; '.' or unknown chars stay transparent. */
function paint(rows: string[], colors: Record<string, string>): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(rows[0].length, rows.length)
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x += 1) {
      const color = colors[row[x]]
      if (color) {
        context.fillStyle = color
        context.fillRect(x, y, 1, 1)
      }
    }
  })
  return canvas
}

/** Blend two hex colors, f = amount of b. */
export function mix(a: string, b: string, f: number): string {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16))
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16))
  const blended = pa.map((channel, i) => Math.round(channel + (pb[i] - channel) * f))
  return `#${blended.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`
}

// ---------------------------------------------------------------- floors

function woodFloorTile(seed: number): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(16, 16)
  const random = mulberry(seed)
  const tones = ['#71553b', '#6a4e35']
  for (let plank = 0; plank < 4; plank += 1) {
    context.fillStyle = tones[(plank + seed) % 2]
    context.fillRect(0, plank * 4, 16, 4)
    context.fillStyle = '#4e3826'
    context.fillRect(0, plank * 4 + 3, 16, 1)
    context.fillRect(Math.floor(random() * 13), plank * 4, 1, 3)
    context.fillStyle = '#5f4630'
    for (let grain = 0; grain < 3; grain += 1) {
      context.fillRect(Math.floor(random() * 12), plank * 4 + Math.floor(random() * 3), 2 + Math.floor(random() * 3), 1)
    }
    context.fillStyle = '#3f2d1e'
    context.fillRect(1 + Math.floor(random() * 3), plank * 4 + 1, 1, 1)
  }
  return canvas
}

function carpetFloorTile(seed: number): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(16, 16)
  const random = mulberry(seed)
  for (let y = 0; y < 16; y += 1) {
    for (let x = 0; x < 16; x += 1) {
      context.fillStyle = (x + y) % 2 ? '#2a3f4d' : '#263a46'
      context.fillRect(x, y, 1, 1)
    }
  }
  context.fillStyle = '#34505e'
  for (let fleck = 0; fleck < 7; fleck += 1) {
    context.fillRect(Math.floor(random() * 16), Math.floor(random() * 16), 1, 1)
  }
  return canvas
}

function metalFloorTile(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(16, 16)
  context.fillStyle = '#262e38'
  context.fillRect(0, 0, 16, 16)
  context.fillStyle = '#313b46'
  context.fillRect(0, 0, 16, 1)
  context.fillRect(0, 8, 16, 1)
  context.fillRect(0, 0, 1, 16)
  context.fillRect(8, 0, 1, 16)
  context.fillStyle = '#1b222a'
  context.fillRect(0, 7, 16, 1)
  context.fillRect(0, 15, 16, 1)
  context.fillRect(7, 0, 1, 16)
  context.fillRect(15, 0, 1, 16)
  context.fillStyle = '#39444f'
  for (const [x, y] of [[2, 2], [12, 2], [2, 12], [12, 12]]) context.fillRect(x, y, 1, 1)
  return canvas
}

function baseFloorPattern(): HTMLCanvasElement {
  const size = 512
  const [canvas, context] = makeCanvas(size, size)
  context.fillStyle = '#0e1520'
  context.fillRect(0, 0, size, size)
  for (let ty = 0; ty < size / 16; ty += 1) {
    for (let tx = 0; tx < size / 16; tx += 1) {
      const roll = mulberry(tx * 73856093 ^ ty * 19349663)()
      if (roll < 0.1) {
        context.fillStyle = roll < 0.05 ? '#101823' : '#0c121a'
        context.fillRect(tx * 16, ty * 16, 16, 16)
      }
      context.fillStyle = '#090e15'
      context.fillRect(tx * 16 + 15, ty * 16, 1, 16)
      context.fillRect(tx * 16, ty * 16 + 15, 16, 1)
      if (roll > 0.9) {
        context.fillStyle = '#1c2836'
        context.fillRect(tx * 16 + 2, ty * 16 + 2, 1, 1)
        context.fillRect(tx * 16 + 13, ty * 16 + 13, 1, 1)
      }
    }
  }
  return canvas
}

// ---------------------------------------------------------------- furniture

function deskSprite(metal: boolean): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(48, 24)
  const top = metal ? '#3a4652' : '#77593d'
  const highlight = metal ? '#4b5a68' : '#8a6b4a'
  const edge = metal ? '#262e37' : '#503a28'
  const line = metal ? '#2f3944' : '#5f4630'
  context.fillStyle = top
  context.fillRect(0, 0, 48, 20)
  context.fillStyle = highlight
  context.fillRect(0, 0, 48, 2)
  context.fillStyle = line
  context.fillRect(0, 6, 48, 1)
  context.fillRect(0, 12, 48, 1)
  context.fillRect(0, 17, 48, 1)
  context.fillRect(15, 2, 1, 18)
  context.fillRect(32, 2, 1, 18)
  context.fillStyle = edge
  context.fillRect(0, 20, 48, 4)
  context.fillStyle = metal ? '#1c232b' : '#3a2a1c'
  context.fillRect(0, 20, 48, 1)
  // keyboard, centered under the monitor position
  context.fillStyle = '#141a20'
  context.fillRect(18, 14, 12, 5)
  context.fillStyle = '#2c3844'
  for (let row = 0; row < 3; row += 1) {
    for (let col = 0; col < 5; col += 1) context.fillRect(19 + col * 2, 15 + row, 1, 1)
  }
  context.fillRect(21, 18, 6, 1)
  return canvas
}

function monitorSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(16, 16)
  context.fillStyle = '#161c23'
  context.fillRect(0, 0, 16, 12)
  context.fillStyle = '#232e39'
  context.fillRect(0, 0, 16, 1)
  context.fillStyle = '#05080b'
  context.fillRect(2, 2, 12, 8)
  context.fillStyle = '#10161d'
  context.fillRect(3, 12, 10, 2)
  context.fillStyle = '#0d1319'
  context.fillRect(6, 14, 4, 2)
  return canvas
}

const screenCache = new Map<string, HTMLCanvasElement>()

/** Animated terminal screen content (12×8), two frames per status. */
export function getScreen(status: StatusKind, frame: number): HTMLCanvasElement {
  const key = `${status}:${frame}`
  const cached = screenCache.get(key)
  if (cached) return cached
  const [canvas, context] = makeCanvas(12, 8)
  const color = STATUS_COLOR[status]
  if (status === 'offline') {
    context.fillStyle = '#0a0d10'
    context.fillRect(0, 0, 12, 8)
    context.fillStyle = '#ffffff12'
    context.fillRect(2, 1, 6, 1)
    context.fillRect(1, 2, 4, 1)
  } else {
    context.fillStyle = '#0a1210'
    context.fillRect(0, 0, 12, 8)
    context.fillStyle = '#060b0a'
    for (let y = 1; y < 8; y += 2) context.fillRect(0, y, 12, 1)
    const widths = status === 'ready' ? [8, 5, 10] : [6, 3, 7]
    widths.forEach((width, row) => {
      context.fillStyle = mix(color, '#0a1210', 0.45)
      context.fillRect(1, row * 2 + 1, width, 1)
      context.fillStyle = color
      context.fillRect(1, row * 2 + 1, 1, 1)
    })
    if (frame === 0) {
      context.fillStyle = color
      context.fillRect(widths[2] + 2 > 10 ? 1 : widths[2] + 2, 7, 2, 1)
    }
  }
  screenCache.set(key, canvas)
  return canvas
}

function rackSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(16, 32)
  context.fillStyle = '#141a21'
  context.fillRect(0, 0, 16, 32)
  context.fillStyle = '#0b0f14'
  context.fillRect(0, 0, 16, 1)
  context.fillRect(0, 0, 1, 32)
  context.fillRect(15, 0, 1, 32)
  context.fillRect(0, 31, 16, 1)
  context.fillStyle = '#232d37'
  for (let x = 3; x < 13; x += 2) {
    context.fillRect(x, 2, 1, 1)
    context.fillRect(x, 3, 1, 1)
  }
  for (let slot = 0; slot < 4; slot += 1) {
    const y = 5 + slot * 6
    context.fillStyle = '#1d2733'
    context.fillRect(2, y, 12, 5)
    context.fillStyle = '#101720'
    context.fillRect(2, y + 4, 12, 1)
    context.fillStyle = '#2f3b47'
    context.fillRect(3, y + 2, 5, 1)
  }
  return canvas
}

/** LED positions inside the rack sprite (2×2 px each). */
export const RACK_LEDS = [0, 1, 2, 3].map((slot) => ({ x: 11, y: 6 + slot * 6 }))

function chairSprite(): HTMLCanvasElement {
  // Facing the desk: seat sliver on top, backrest toward the viewer,
  // pedestal peeking out below.
  return paint([
    '....UUUU....',
    '..uUUUUUUu..',
    '.uUUUUUUUUu.',
    '.uUUUUUUUUu.',
    '.uUUUUUUUUu.',
    '.uUUUUUUUUu.',
    '.uUUUUUUUUu.',
    '..uuuuuuuu..',
    '.....uu.....',
    '.....uu.....',
    '....uuuu....',
    '..uu....uu..',
  ], { u: '#2c3947', U: '#344453' })
}

function rugSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(64, 40)
  const random = mulberry(9241)
  context.fillStyle = '#24414c'
  context.fillRect(0, 0, 64, 40)
  context.fillStyle = '#33596b'
  context.fillRect(0, 0, 64, 2)
  context.fillRect(0, 38, 64, 2)
  context.fillRect(0, 0, 2, 40)
  context.fillRect(62, 0, 2, 40)
  context.fillStyle = '#1d3540'
  context.fillRect(4, 4, 56, 1)
  context.fillRect(4, 35, 56, 1)
  context.fillRect(4, 4, 1, 32)
  context.fillRect(59, 4, 1, 32)
  context.fillStyle = '#3c6a80'
  for (const [cx, cy] of [[32, 20], [14, 20], [50, 20]]) {
    context.fillRect(cx - 1, cy - 3, 2, 1)
    context.fillRect(cx - 2, cy - 2, 4, 1)
    context.fillRect(cx - 3, cy - 1, 6, 2)
    context.fillRect(cx - 2, cy + 1, 4, 1)
    context.fillRect(cx - 1, cy + 2, 2, 1)
  }
  context.fillStyle = '#2a4a58'
  for (let wear = 0; wear < 26; wear += 1) {
    context.fillRect(3 + Math.floor(random() * 58), 3 + Math.floor(random() * 34), 1, 1)
  }
  return canvas
}

const PLANT_A = paint([
  '.....gg.....',
  '....gGGg....',
  '...gGGGGg...',
  '..gGgGGgGg..',
  '..gGGGGGGg..',
  '.gGgGGGGgGg.',
  '.gGGgGGgGGg.',
  '..gGGGGGGg..',
  '...gGGGGg...',
  '...gGgGGg...',
  '....gggg....',
  '...rrrrrr...',
  '..rrRRRRrr..',
  '..rRRRRRRr..',
  '..rRRRRRRr..',
  '..rRRRRRRr..',
  '...rrrrrr...',
], { g: '#2f623d', G: '#54a06a', r: '#6e4129', R: '#8f5a40' })

const PLANT_B = paint([
  '....g.G.....',
  '....g.G..G..',
  '..G.g.G..G..',
  '..G.g.Gg.G..',
  '..Gg.gG..G..',
  '..Gg.Gg..Gg.',
  '.gGg.Gg..Gg.',
  '.gGggG...Gg.',
  '.gGGGg...gG.',
  '.gGGGg..gG..',
  '.gGGGgg.gG..',
  '.gGGGGg.gG..',
  '..GGGGggG...',
  '...rrrrrr...',
  '..rrRRRRrr..',
  '..rRRRRRRr..',
  '...rrrrrr...',
], { g: '#2a5a38', G: '#4d9160', r: '#26313c', R: '#37434f' })

function shelfSprite(seed: number): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(24, 24)
  const random = mulberry(seed)
  const books = ['#b3674a', '#58a6c9', '#c9c158', '#7d5a94', '#3f8a6c', '#c9588a', '#8a8f98']
  context.fillStyle = '#5d4330'
  context.fillRect(0, 0, 24, 24)
  context.fillStyle = '#3a2b1e'
  context.fillRect(2, 2, 20, 20)
  for (const shelfY of [3, 13]) {
    let x = 3
    while (x < 19) {
      if (random() < 0.18) {
        x += 2
        continue
      }
      const width = 1 + Math.floor(random() * 2)
      const height = 5 + Math.floor(random() * 3)
      context.fillStyle = books[Math.floor(random() * books.length)]
      context.fillRect(x, shelfY + 7 - height, width, height)
      x += width + 1
    }
    context.fillStyle = '#6e5138'
    context.fillRect(2, shelfY + 7, 20, 2)
  }
  return canvas
}

function lampSprite(): HTMLCanvasElement {
  return paint([
    '..mmmmmm..',
    '.mmmmmmmm.',
    '.mMMMMMMm.',
    '..mMMMMm..',
    '...wwww...',
    '....pp....',
    '....pp....',
    '....pp....',
    '....pp....',
    '....pp....',
    '....pp....',
    '....pp....',
    '....pp....',
    '..bbbbbb..',
    '..bbbbbb..',
  ], { m: '#37424d', M: '#4b5a68', w: '#ffd9a0', p: '#232e39', b: '#2c3947' })
}

function acSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(20, 10)
  context.fillStyle = '#333e49'
  context.fillRect(0, 0, 20, 10)
  context.fillStyle = '#46525f'
  context.fillRect(0, 0, 20, 1)
  context.fillStyle = '#232c35'
  context.fillRect(2, 5, 15, 1)
  context.fillRect(2, 7, 15, 1)
  context.fillStyle = '#77f2b4'
  context.fillRect(17, 2, 2, 1)
  return canvas
}

function crateSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(16, 16)
  context.fillStyle = '#1d2833'
  context.fillRect(0, 0, 16, 16)
  context.fillStyle = '#2b3947'
  context.fillRect(0, 0, 16, 1)
  context.fillRect(0, 0, 1, 16)
  context.fillStyle = '#141c25'
  context.fillRect(0, 15, 16, 1)
  context.fillRect(15, 0, 1, 16)
  context.fillRect(3, 3, 10, 1)
  context.fillRect(3, 12, 10, 1)
  context.fillRect(3, 3, 1, 10)
  context.fillRect(12, 3, 1, 10)
  return canvas
}

function pipeSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(24, 8)
  context.fillStyle = '#232f3c'
  context.fillRect(0, 2, 24, 4)
  context.fillStyle = '#313f4e'
  context.fillRect(0, 2, 24, 1)
  context.fillStyle = '#18212b'
  context.fillRect(0, 1, 2, 6)
  context.fillRect(20, 1, 2, 6)
  context.fillStyle = '#141c25'
  context.fillRect(22, 3, 2, 2)
  return canvas
}

// ---------------------------------------------------------------- wall decor

function windowSprite(variant: number, seed: number): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(24, 12)
  const random = mulberry(seed)
  context.fillStyle = '#4a6076'
  context.fillRect(0, 0, 24, 12)
  context.fillStyle = '#14263a'
  context.fillRect(2, 2, 20, 8)
  if (variant === 0) {
    context.fillStyle = '#bfe4ff'
    for (let star = 0; star < 5; star += 1) {
      context.fillRect(3 + Math.floor(random() * 17), 3 + Math.floor(random() * 5), 1, 1)
    }
    context.fillStyle = '#f4faff'
    context.fillRect(14, 3, 4, 5)
    context.fillStyle = '#14263a'
    context.fillRect(17, 3, 1, 5)
    context.fillRect(14, 7, 4, 1)
  } else {
    context.fillStyle = '#2a405a'
    for (let y = 3; y < 10; y += 2) context.fillRect(2, y, 20, 1)
  }
  context.fillStyle = '#3a4e62'
  context.fillRect(11, 2, 2, 8)
  context.fillStyle = '#33455a'
  context.fillRect(0, 10, 24, 2)
  return canvas
}

const POSTER_A = paint([
  'ffffffffffff',
  'fppppppppppf',
  'fppppppppppf',
  'foooooooooof',
  'foooooooooof',
  'fooooyyyooof',
  'fooooyyyooof',
  'fvvvvvvvvvvf',
  'fvvvvvvvvvvf',
  'fddddddddddf',
  'fddddddddddf',
  'ffffffffffff',
], { f: '#3d5265', p: '#7d5a94', o: '#b3674a', y: '#e8c46a', v: '#4a3a5e', d: '#2c2337' })

const POSTER_B = paint([
  '............',
  '.yy...cc....',
  '.yy...cc..pp',
  '.....c...pp.',
  '.ss......pp.',
  '.ss..mm.....',
  '.....mm..yy.',
  '.........yy.',
  '..pp........',
  '..pp..cc....',
  '......cc....',
  '............',
], { y: '#c9c158', c: '#58a6c9', p: '#c9588a', s: '#3f8a6c', m: '#c98a52' })

// ---------------------------------------------------------------- fx

const ZZZ = paint([
  'zzzzz',
  '...z.',
  '..z..',
  '.z...',
  'zzzzz',
], { z: '#9fc6e8' })

const ALERT = paint([
  '..wwwwww..',
  '.wwwwwwww.',
  '.wwwxxwww.',
  '.wwwxxwww.',
  '.wwwxxwww.',
  '.wwwxxwww.',
  '.wwwwwwww.',
  '.wwwxxwww.',
  '.wwwwwwww.',
  '..wwwwww..',
  '.ww.......',
  'ww........',
], { w: '#e8f0f8', x: '#e0483f' })

const SPARKLE_A = paint([
  '....y....',
  '....y....',
  '....y....',
  '...yyy...',
  'yyyyyyyyy',
  '...yyy...',
  '....y....',
  '....y....',
  '....y....',
], { y: '#ffd9a0' })

const SPARKLE_B = paint([
  'y.......y',
  '.y.....y.',
  '..y...y..',
  '...y.y...',
  '....y....',
  '...y.y...',
  '..y...y..',
  '.y.....y.',
  'y.......y',
], { y: '#ffd9a0' })

function roomShadowSprite(): HTMLCanvasElement {
  const [canvas, context] = makeCanvas(236, 172)
  context.fillStyle = 'rgba(0,0,0,.38)'
  context.beginPath()
  context.roundRect(2, 2, 230, 166, 8)
  context.fill()
  context.fillStyle = 'rgba(0,0,0,.16)'
  context.beginPath()
  context.roundRect(0, 5, 234, 166, 10)
  context.fill()
  return canvas
}

// ---------------------------------------------------------------- characters

const SKINS = ['#f0c8a0', '#d9a06e', '#a5754b']
const HAIRS = ['#2b2118', '#6b4a2f', '#b8894a', '#7d8894']
const CAPS = ['#b34a3e', '#3e6db3', '#3e8a57']
const SHIRTS = ['#3f8a6c', '#4a6f9d', '#7d5a94', '#96684a', '#4a7d88']
const PANTS = ['#232b34', '#2e3742', '#3a3230']

export type CharPose = 'sit' | 'stand'

const charCache = new Map<string, HTMLCanvasElement>()

function charPalettes(variant: number, active: boolean) {
  const dim = (color: string) => mix(color, '#4a545e', 0.55)
  const pick = <T,>(list: T[], shift: number): T => list[(variant >>> shift) % list.length]
  const palette = {
    skin: pick(SKINS, 0),
    hair: pick(HAIRS, 2),
    cap: pick(CAPS, 2),
    shirt: pick(SHIRTS, 5),
    pants: pick(PANTS, 8),
    glow: active ? '#8affd2' : '#4a5a64',
  }
  if (active) return palette
  return { ...palette, skin: dim(palette.skin), hair: dim(palette.hair), cap: dim(palette.cap), shirt: dim(palette.shirt), pants: dim(palette.pants) }
}

function standRows(style: number, frame: number, blink: boolean): string[] {
  const head = [
    '....hhhh....',
    '...hhhhhh...',
    '..hhhhhhhh..',
    '..hhsssshh..',
    blink ? '..hssssssh..' : '..hsessesh..',
    '..hssssssh..',
    '...ssssss...',
  ]
  if (style === 1) {
    head[0] = '.hh.hhhh.hh.'
    head[1] = '..hhhhhhhh..'
  } else if (style === 2) {
    head[3] = '..hhhhhhhh..'
    head[6] = '..hssssssh..'
  } else if (style === 3) {
    head[0] = '....kkkk....'
    head[1] = '...kkkkkk...'
    head[2] = '..kkkkkkkk..'
    head[3] = '..kksssskk..'
  }
  const torsoA = [
    '..cccccccc..',
    '.cccccccccc.',
    '.cctggggtcc.',
    '.cctggggtcc.',
    '..cccccccc..',
  ]
  const torsoB = [
    '..cccccccc..',
    '.cccccccccc.',
    '.cccccccccc.',
    '.cctggggtcc.',
    '.cctggggtcc.',
  ]
  return [
    ...head,
    ...(frame === 0 ? torsoA : torsoB),
    '..cccccccc..',
    '...pp..pp...',
    '...pp..pp...',
    '...oo..oo...',
  ]
}

function sitRows(style: number, frame: number): string[] {
  // Back view, seated on the chair and facing the desk: hands rest on the
  // keyboard, the chair backrest wraps the lower back and the pedestal
  // peeks out below. 14×23, chair included so layering is always correct.
  const hands = frame === 0 ? '..ss......ss..' : '...ss....ss...'
  const rows = [
    hands,
    hands,
    '..aahhhhhhaa..',
    '..aahhhhhhaa..',
    '..aahhhhhhaa..',
    '..aahhhhhhaa..',
    '..aahhhhhhaa..',
    '...hhhhhhhh...',
    '.....ssss.....',
    '..cccccccccc..',
    '..cccccccccc..',
    '..cccccccccc..',
    '..cccccccccc..',
    '.uUUUUUUUUUUu.',
    '.uUUUUUUUUUUu.',
    '.uUUUUUUUUUUu.',
    '.uUUUUUUUUUUu.',
    '..uuuuuuuuuu..',
    '...pppppppp...',
    '......uu......',
    '......uu......',
    '.....uuuu.....',
    '...uu....uu...',
  ]
  if (style === 1) {
    // tufty hair
    rows[2] = '..aah.hh.haa..'
  } else if (style === 2) {
    // long hair falling down the back
    rows[8] = '....hssssh....'
    rows[9] = '..hcccccccch..'
    rows[10] = '..hcccccccch..'
    rows[11] = '..hcccccccch..'
  } else if (style === 3) {
    // cap, hair peeking out under it
    rows[2] = '..aakkkkkkaa..'
    rows[3] = '..aakkkkkkaa..'
    rows[4] = '..aakkkkkkaa..'
    rows[5] = '..aakkhhkkaa..'
  }
  return rows
}

/**
 * Chibi character sprite. Standing characters face the viewer holding a
 * glowing tablet; sitting characters are seen from behind, seated on the
 * desk chair with their hands on the keyboard (the chair is part of the
 * sprite so the layering always reads correctly).
 */
export function getCharacter(pose: CharPose, variant: number, frame: number, blink: boolean, active: boolean): HTMLCanvasElement {
  const style = (variant >>> 10) % 4
  const key = `${pose}:${variant % 2048}:${frame}:${blink ? 1 : 0}:${active ? 1 : 0}`
  const cached = charCache.get(key)
  if (cached) return cached
  const palette = charPalettes(variant, active)
  const rows = pose === 'sit' ? sitRows(style, frame) : standRows(style, frame, blink)
  const sprite = paint(rows, {
    h: palette.hair,
    k: palette.cap,
    s: palette.skin,
    e: '#20262e',
    c: palette.shirt,
    a: mix(palette.shirt, '#ffffff', 0.18),
    t: '#10161c',
    g: palette.glow,
    p: palette.pants,
    o: '#161c23',
    u: '#2c3947',
    U: '#344453',
  })
  charCache.set(key, sprite)
  return sprite
}

// ---------------------------------------------------------------- nameplate

/** Pixelated floor plaque with the device name and port (CJK-safe). */
export function makeFloorLabel(name: string, port: number, status: StatusKind): HTMLCanvasElement {
  const accent = STATUS_COLOR[status]
  const label = name.length > 14 ? `${name.slice(0, 13)}…` : name
  const portLabel = `:${port}`
  const [probe, probeContext] = makeCanvas(1, 1)
  probeContext.font = '700 9px Manrope, system-ui, sans-serif'
  const nameWidth = Math.ceil(probeContext.measureText(label).width)
  probeContext.font = '500 7px "DM Mono", ui-monospace, monospace'
  const portWidth = Math.ceil(probeContext.measureText(portLabel).width)
  const textWidth = Math.max(nameWidth + 8, portWidth)
  const width = textWidth + 14
  const height = 26

  const [canvas, context] = makeCanvas(width, height)
  // semi-transparent plaque so the floor texture shows through
  context.fillStyle = 'rgba(13,19,26,.88)'
  context.fillRect(0, 0, width, height)
  context.fillStyle = '#2a3a47'
  context.fillRect(0, 0, width, 1)
  context.fillRect(0, height - 1, width, 1)
  context.fillRect(0, 0, 1, height)
  context.fillRect(width - 1, 0, 1, height)
  context.fillStyle = '#1d2b38'
  context.fillRect(1, 1, width - 2, 1)
  // status accent line + dot
  context.fillStyle = accent
  context.fillRect(2, height - 3, width - 4, 2)
  context.fillRect(5, 6, 3, 3)
  // pixelated text
  context.textBaseline = 'top'
  context.font = '700 9px Manrope, system-ui, sans-serif'
  context.fillStyle = '#e8f2f8'
  context.fillText(label, 11, 3)
  context.font = '500 7px "DM Mono", ui-monospace, monospace'
  context.fillStyle = '#7d8b98'
  context.fillText(portLabel, Math.floor((width - portWidth) / 2), 15)
  return canvas
}

// ---------------------------------------------------------------- atlas

export interface Atlas {
  floors: HTMLCanvasElement[]
  baseFloor: HTMLCanvasElement
  desks: HTMLCanvasElement[]
  monitor: HTMLCanvasElement
  rack: HTMLCanvasElement
  chair: HTMLCanvasElement
  rug: HTMLCanvasElement
  plants: HTMLCanvasElement[]
  shelf: HTMLCanvasElement
  lamp: HTMLCanvasElement
  ac: HTMLCanvasElement
  crate: HTMLCanvasElement
  pipe: HTMLCanvasElement
  windows: HTMLCanvasElement[]
  posters: HTMLCanvasElement[]
  zzz: HTMLCanvasElement
  alert: HTMLCanvasElement
  sparkles: HTMLCanvasElement[]
  roomShadow: HTMLCanvasElement
}

let atlasInstance: Atlas | null = null

export function getAtlas(): Atlas {
  if (atlasInstance) return atlasInstance
  atlasInstance = {
    floors: [woodFloorTile(3), carpetFloorTile(7), metalFloorTile()],
    baseFloor: baseFloorPattern(),
    desks: [deskSprite(false), deskSprite(true)],
    monitor: monitorSprite(),
    rack: rackSprite(),
    chair: chairSprite(),
    rug: rugSprite(),
    plants: [PLANT_A, PLANT_B],
    shelf: shelfSprite(11),
    lamp: lampSprite(),
    ac: acSprite(),
    crate: crateSprite(),
    pipe: pipeSprite(),
    windows: [windowSprite(0, 5), windowSprite(1, 9)],
    posters: [POSTER_A, POSTER_B],
    zzz: ZZZ,
    alert: ALERT,
    sparkles: [SPARKLE_A, SPARKLE_B],
    roomShadow: roomShadowSprite(),
  }
  return atlasInstance
}
