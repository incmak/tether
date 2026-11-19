---
status: accepted
---

# Urgent calls go through a Graph calling bot in BriskBot

When an agent hits a critical blocker, Tether rings the user on Teams. BriskBot places the call through Microsoft Graph cloud communications (`POST /communications/calls`, service-hosted media). Tether stays reachable only on its private network and drives the call through an outbound link to BriskBot. We chose this because it needs no paid telephony, BriskBot already has a public HTTPS host and the user's Teams chat, and Tether never opens a public endpoint (backlog B350, decided 19 Nov 2026).

## Considered options

- **Paid phone number service (PSTN, for example Twilio).** Rejected: it costs money for each call and each month, and the Friday voice stack is free-only by decision.
- **Azure Communication Services Call Automation.** Rejected: calls to a Teams user need a Teams Phone license, Teams iOS and Android are not supported, and Microsoft retires the standalone service by 30 Sep 2028.
- **A public calling webhook on Tether itself.** Rejected: Tether controls real terminals, and a public endpoint would add an attack surface for that.
- **Application-hosted media, so Friday talks live in the call.** Rejected: Microsoft supports it only for a Windows Server .NET bot on Azure. Friday runs in Python on Linux.

## Consequences

- The call works in turns, not as a live talk: Tether plays a WAV in Friday's voice, records up to 120 seconds, and reads keypad tones. Each turn has a few seconds of delay.
- Blocker text leaves the private network as audio, because Microsoft fetches each prompt WAV by URL. The WAV URLs are unguessable, expire after 10 minutes, and are deleted when the call ends. Recordings are deleted after transcription.
- BriskBot must validate the bearer token on every calling webhook request before it acts, so a forged event never reaches a tab.
- The tenant needs admin consent for `Calls.Initiate.All` and `Calls.AccessMedia.All`.
