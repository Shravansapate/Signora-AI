import './globals.css';

export const metadata = {
  title: 'Signora | ISL content review',
  description: 'Authenticated Indian Sign Language motion review and persistent avatar playback.',
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }) {
  return <html lang="en"><body>{children}</body></html>;
}
