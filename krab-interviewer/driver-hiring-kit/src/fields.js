export const FIELDS = [
  {
    key: "full_name",
    label: "Full name",
    sublabel: "Legal name as it appears on your driver’s license.",
    required: true,
    placeholder: "Eddie Goldman",
  },
  {
    key: "work_commitment",
    label: "Work commitment",
    sublabel: "How many years would you like to work together?",
    required: true,
    choices: ["1", "2", "3", "Forever", "I Don't want to work I just need money"],
  },
  {
    key: "phone_number",
    label: "Phone Number",
    sublabel: "Best Phone number you can be reached to receive daily deliveries and client calls.",
    required: true,
    type: "tel",
    placeholder: "(201) 555-0123",
  },
  {
    key: "email",
    label: "Email",
    sublabel: "Active email address.",
    required: true,
    type: "email",
    placeholder: "eddie.goldman@gmail.com",
  },
  {
    key: "mailing_address",
    label: "Mailing address",
    sublabel:
      "Where would you like us to mail your Test batch of 10 DMV Motor Vehicle Temporary Tag Papers for your 1st 10 deliveries? 10 DELIVERIES x $50 USD = $500",
    required: true,
    textarea: true,
    placeholder: "Your Full Name\n321 Main Street\nFort Lee New Jersey 07024",
  },
  {
    key: "telegram_username",
    label: "Telegram Username (@username)",
    setupGuide: [
      {
        title: "1. Download the Telegram Messenger app:",
        appLinks: [
          {
            label: "📱 iPhone:",
            href: "https://apps.apple.com/app/telegram-messenger/id686449807",
          },
          {
            label: "🤖 Android:",
            href: "https://play.google.com/store/apps/details?id=org.telegram.messenger",
          },
        ],
      },
      {
        text: "2. Open Telegram and create an account.",
      },
      {
        title: "3. Set your username:",
        lines: [
          "Go to Settings",
          "Tap Edit Profile",
          "Tap Username",
          "Create a username that starts with @ (example: @johnsmith)",
        ],
        video: "/videos/telegram-username-tutorial.mp4",
        videoCaption: "Tutorial: how to set your @username in Telegram",
      },
      {
        text: "4. Enter your Telegram username (@username) in the field below.",
      },
    ],
    required: true,
    highlight: true,
    placeholder: "your_username",
    autoResolveTelegram: true,
  },
  {
    key: "emergency_contact",
    label: "Family Emergency contact",
    sublabel: "Name and phone number of any family member or emergency contact",
    required: true,
    placeholder: "Jane Goldman — (201) 555-9999",
  },
  {
    key: "referral",
    label: "Referral (optional)",
    sublabel: "Who told you about this job?",
    required: false,
    placeholder: "Friend name, website, or —",
  },
  {
    key: "payment_method",
    label: "Payment Preference",
    sublabel: "How do you want to get paid for deliveries?",
    required: true,
    choices: ["Zelle", "Cashapp", "Venmo", "PayPal"],
    companionKey: "payment_id",
  },
  {
    key: "payment_id",
    label: "Payment method ID",
    sublabel:
      "Your Zelle phone/email, CashApp $tag, Venmo @username, or PayPal email — where we send $50 per delivery.",
    required: true,
    nested: true,
    placeholder: "e.g. 732-241-8338 or $YourCashTag or @YourVenmo",
  },
  {
    key: "profession_skill",
    label: "Profession / Skills",
    sublabel: "What are you good at, your talents?",
    required: true,
    placeholder: "Delivery driver, customer service…",
  },
];

/** Keys the web form accepts from AI parse / autosave */
export const FORM_FIELD_KEYS = [...FIELDS.map((f) => f.key), "telegram_id"];
