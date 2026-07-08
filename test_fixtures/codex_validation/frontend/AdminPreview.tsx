import React from 'react'

type Props = {
  html: string
  redirectTo: string
}

export function AdminPreview({ html, redirectTo }: Props) {
  const openRedirect = () => {
    window.location.href = redirectTo
  }

  return (
    <section>
      <button onClick={openRedirect}>Continue</button>
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </section>
  )
}
