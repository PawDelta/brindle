export const PRESENCE_TTL = 30_000;
export const PEER_COLOURS = 9;
export const peerClass = (name: string) => `loro-peer-${[...name].reduce((h, c) => (h * 31 + c.codePointAt(0)!) >>> 0, 7) % PEER_COLOURS}`;
