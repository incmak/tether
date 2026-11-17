// One row as the user saw it: its text, and its detail line when it has one. Two Rewind points with the same text differ
// by their detail. The server builds the same key (picker_row_key in terminal_input.py) and refuses when they differ.
export const pickRowKey = (row: { text: string; detail?: string | null }) => row.text + (row.detail ? '\n' + row.detail : '')
// The name of the list the user saw. The server builds the same name (picker_heading) and refuses another list.
export const pickHeading = (list: { reason?: string; question?: string | null; title?: string | null; text?: string | null }) =>
  list.reason === 'menu' ? list.question || '' : list.title || (list.text || '').split('\n')[0].trim()
