import { Link } from "@tanstack/react-router"

import { useTheme } from "@/components/theme-provider"
import { cn } from "@/lib/utils"
import icon from "/assets/images/patch-and-pantry-icon.svg"
import iconLight from "/assets/images/patch-and-pantry-icon-light.svg"

const APP_NAME = "Patch & Pantry"

interface LogoProps {
  variant?: "full" | "icon" | "responsive"
  className?: string
  asLink?: boolean
}

export function Logo({
  variant = "full",
  className,
  asLink = true,
}: LogoProps) {
  const { resolvedTheme } = useTheme()
  const iconSrc = resolvedTheme === "dark" ? iconLight : icon

  // The full logo is the icon plus the name; its height (h-*) and text size (text-*) come from
  // className, so callers can scale both together.
  const full = (extra?: string) => (
    <span
      className={cn(
        "inline-flex h-6 items-center gap-2 text-lg",
        extra,
        className,
      )}
    >
      <img src={iconSrc} alt="" className="h-full w-auto" />
      <span className="font-brand font-semibold leading-none tracking-tight whitespace-nowrap">
        {APP_NAME}
      </span>
    </span>
  )

  const iconOnly = (extra?: string) => (
    <img
      src={iconSrc}
      alt={APP_NAME}
      className={cn("size-5", extra, className)}
    />
  )

  const content =
    variant === "responsive" ? (
      <>
        {full("group-data-[collapsible=icon]:hidden")}
        {iconOnly("hidden group-data-[collapsible=icon]:block")}
      </>
    ) : variant === "full" ? (
      full()
    ) : (
      iconOnly()
    )

  if (!asLink) {
    return content
  }

  return (
    <Link to="/" aria-label={APP_NAME}>
      {content}
    </Link>
  )
}
