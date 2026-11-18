import { useState } from 'react'
import { ImageOff } from 'lucide-react'
import { ImageViewer } from './ImageViewer'

type Shot = { name: string; url: string }

// The composer puts an upload's path into the message (server/uploads.py). Only image files become thumbnails; other files stay as text.
const UPLOAD = /(^|\s)(\S*\/mobile-tmux\/uploads\/[A-Za-z0-9._-]+\.(?:png|jpe?g|gif|webp))(?=\s|$)/gi

export function imagesOnly(text: string): boolean {
  const { paths, rest } = uploadImageParts(text)
  return paths.length > 0 && !rest
}

export function uploadImageParts(text: string) {
  const paths: string[] = []
  const rest = text.replace(UPLOAD, (_, before: string, path: string) => { paths.push(path); return before })
  return { paths, rest: rest.trim() }
}

export function sentContent(text: string, session: string, images = 0, imageKey?: string): { shots: Shot[]; rest: string } {
  const shots: Shot[] = []
  // Claude swaps a message that is only image paths for "[Image #N]" and keeps the pictures in its transcript.
  if (images > 0 && imageKey) {
    for (let n = 0; n < images; n++) shots.push({ name: `Image ${n + 1}`, url: `/mobile/api/message-image?session=${encodeURIComponent(session)}&key=${encodeURIComponent(imageKey)}&n=${n}` })
    text = text.replace(/\[Image #\d+\]/g, '')
  }
  const rest = text.replace(UPLOAD, (_, before: string, path: string) => {
    // Uploads start with a random id ("6a3651d2-image.png"); the person never chose it.
    shots.push({ name: (path.split('/').pop() ?? path).replace(/^[0-9a-f]{8}-/, ''), url: `/mobile/api/file?session=${encodeURIComponent(session)}&path=${encodeURIComponent(path)}` })
    return before
  })
  return { shots, rest: rest.trim() }
}

function SentImage({ shot, open }: { shot: Shot; open: () => void }) {
  const [gone, setGone] = useState(false)
  // Uploads are deleted after a week, so an old message can name a file that no longer exists.
  if (gone) return <li className="sent-image-gone"><ImageOff size={16} aria-hidden="true" /><span>{shot.name}</span><span>No longer available</span></li>
  return <li><button type="button" aria-label={`View ${shot.name}`} onClick={open}><img src={shot.url} alt="" loading="lazy" onError={() => setGone(true)} /><span>{shot.name}</span></button></li>
}

/** A sent message: its pictures as thumbnails, then the person's words. */
export function SentContent({ shots, rest }: { shots: Shot[]; rest: string }) {
  const [viewing, setViewing] = useState<Shot | null>(null)
  return <>
    {shots.length > 0 && <ul className="tool-images sent-images" aria-label="Images">{shots.map(shot => <SentImage key={shot.url} shot={shot} open={() => setViewing(shot)} />)}</ul>}
    {rest && <span>{rest}</span>}
    <ImageViewer image={viewing} close={() => setViewing(null)} items={shots} onSelect={setViewing} />
  </>
}
