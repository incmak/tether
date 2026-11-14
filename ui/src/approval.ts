// Passkey approval (server: approval.py). Agents can call Tether's API with its token, so a click is no proof that the
// person approved. Actions that give agents more power carry a WebAuthn assertion with user verification instead.
import { ApiError } from './api'
import type { Api } from './api'

export type ApprovalAction = 'setup-hook' | 'setup-ping' | 'decision-save' | 'level-raise' | 'passkey-add' | 'passkey-remove' | 'lesson-promote'
export interface Passkey { id: string; name: string; created: string; last_used: string | null }
export interface ApprovalStatus { passkeys: Passkey[]; here: number }
type Options = { challenge_id: string; publicKey: Record<string, unknown> }
type Approval = { challenge_id: string; credential: Record<string, unknown> }

const toBytes = (text: string) => Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(text.length / 4) * 4, '=')), char => char.charCodeAt(0))
const toText = (buffer: ArrayBuffer | null) => buffer ? btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') : undefined

export const passkeysWork = () => typeof window !== 'undefined' && window.isSecureContext && !!window.PublicKeyCredential && !!navigator.credentials

const withoutApproval = (body: Record<string, unknown>) => Object.fromEntries(Object.entries(body).filter(([key]) => key !== 'approval'))

function cancelled(error: unknown) {
  return error instanceof DOMException && (error.name === 'NotAllowedError' || error.name === 'AbortError')
}

/** Asks the browser for a passkey assertion bound to this exact action and payload. */
export async function approve(api: Api, action: ApprovalAction, payload: Record<string, unknown>): Promise<Approval> {
  if (!passkeysWork()) throw new Error('This browser cannot use passkeys here. Open Tether over HTTPS.')
  const options = await api<Options>('passkeys', { op: 'options', kind: 'get', action, payload })
  const key = options.publicKey as { challenge: string; allowCredentials?: { id: string; type: string }[] } & Record<string, unknown>
  let credential: PublicKeyCredential | null
  try {
    credential = await navigator.credentials.get({ publicKey: {
      ...key, challenge: toBytes(key.challenge),
      allowCredentials: key.allowCredentials?.map(item => ({ ...item, id: toBytes(item.id), type: 'public-key' as const })),
    } as PublicKeyCredentialRequestOptions }) as PublicKeyCredential | null
  } catch (error) {
    if (cancelled(error)) throw new Error('You cancelled the passkey check, so nothing changed.', { cause: error })
    throw error
  }
  if (!credential) throw new Error('The passkey check did not finish, so nothing changed.')
  const response = credential.response as AuthenticatorAssertionResponse
  return { challenge_id: options.challenge_id, credential: { id: credential.id, rawId: toText(credential.rawId), type: credential.type, response: {
    clientDataJSON: toText(response.clientDataJSON), authenticatorData: toText(response.authenticatorData),
    signature: toText(response.signature), userHandle: toText(response.userHandle),
  } } }
}

/** Sends the request; when the server asks for approval, runs the passkey check and sends it once more. */
export async function withApproval<T>(api: Api, path: string, action: ApprovalAction, body: Record<string, unknown>): Promise<T> {
  try { return await api<T>(path, body) }
  catch (error) {
    if (!(error instanceof ApiError && error.approval)) throw error
  }
  const payload = withoutApproval(body)
  return api<T>(path, { ...payload, approval: await approve(api, action, payload) })
}

/** Adds a passkey on this device. A second passkey needs approval with one that exists. */
export async function addPasskey(api: Api, name: string, existing: boolean) {
  if (!passkeysWork()) throw new Error('This browser cannot use passkeys here. Open Tether over HTTPS.')
  const approval = existing ? await approve(api, 'passkey-add', { name }) : undefined
  const options = await api<Options>('passkeys', { op: 'options', kind: 'create', action: 'passkey-add', payload: { name } })
  const key = options.publicKey as { challenge: string; user: { id: string } & Record<string, unknown>; excludeCredentials?: { id: string; type: string }[] } & Record<string, unknown>
  let credential: PublicKeyCredential | null
  try {
    credential = await navigator.credentials.create({ publicKey: {
      ...key, challenge: toBytes(key.challenge), user: { ...key.user, id: toBytes(key.user.id) },
      excludeCredentials: key.excludeCredentials?.map(item => ({ ...item, id: toBytes(item.id), type: 'public-key' as const })),
    } as unknown as PublicKeyCredentialCreationOptions }) as PublicKeyCredential | null
  } catch (error) {
    if (cancelled(error)) throw new Error('You cancelled, so Tether did not add a passkey.', { cause: error })
    if (error instanceof DOMException && error.name === 'InvalidStateError') throw new Error('This device already has a Tether passkey.', { cause: error })
    throw error
  }
  if (!credential) throw new Error('Tether did not add a passkey.')
  const response = credential.response as AuthenticatorAttestationResponse
  return api<ApprovalStatus>('passkeys', { op: 'enroll', challenge_id: options.challenge_id, name, approval,
    credential: { id: credential.id, rawId: toText(credential.rawId), type: credential.type, response: {
      clientDataJSON: toText(response.clientDataJSON), attestationObject: toText(response.attestationObject) } } })
}

export async function removePasskey(api: Api, id: string) {
  const approval = await approve(api, 'passkey-remove', { id })
  return api<ApprovalStatus>('passkeys', { op: 'remove', id, approval })
}
