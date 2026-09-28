'use client';

export default function ErrorPage({ reset }) {
  return <main className="fatal-error"><h1>The review workspace could not continue.</h1>
    <p>Reload the workspace to create a fresh playback session.</p>
    <button className="primary" onClick={reset}>Reload workspace</button></main>;
}
