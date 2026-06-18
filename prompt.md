Create a Windows Child cybersecurity Safety App that can be installed.This app targets kids 16 and under.
The app should have a UI that creates a window and requires a parent password that the user can create when the app is first installed and can be changed later. This UI will alter the settings and the changes to the settings are automatically saved and applied. The UI should:
Have the parent input their phone number that will later receive automated warnings (should be left blank if the parent does not want to receive warnings)
Toggle to enable or disable screen monitoring for online safety and warnings. If this is not enabled then the other settings below will be useless and faded out. If it is enabled then the user can toggle the other settings below
Toggle warnings for different categories of suspicious messages that are listed below
Toggle notifications to the parent’s phone number
These actions will always run if enabled in the app settings.
Gives warning messages to children about inappropriate behavior and activities. 
The warning:
Popup in the corner of the screen with a raccoon holding a magnifying glass and giving a warning message (ie dont trust strangers! Keep you personal information to yourself) (ie dont repeat everything you see online) (ie not everything you see online is true) 
The screen will darken to give focus to the warning message and will remain for 5 seconds before allowing the user to exit out
Warning is given when certain keywords or phrases are detected
Warning cannot be dismissed for 5 seconds so the kid reads it
Sends an automated message to the parent's phone number with a screenshot of the screen if the parent enabled notifications sent to their phone.
Use the free Geminin API key that detects suspicious messages (looks at onscreen text) and gives a warning to the kid. This should look for messages that ask for personal data, contain explicit content, and seem like “social engineering” threats that could tempt the child user to jeopardize their cybersecurity. Some examples of suspicious messages include: where do you live, Click here to pay a $3.00 redelivery fee, what is your password, OMG! I just got 10,000 free Robux/V-Bucks for [Roblox/Fortnite]! Just click this link and put in your username and password to claim yours. Hurry, it expires in an hour! Or Hey! I am locked out of my main account and can't log in. I just had a security code sent to your phone. Can you text it to me so I can get back in?” The AI should look at messages or keywords that are associated with cybersecurity threats, phishing, and social engineering that target children in video games and live chats. These are some examples, the AI should also look at the categories below and if keywords or strings of phrases are associated with those categories, the Ai should create a warning that advises the child user to be safe online, not share personal data, not to mimic everything you see people do online, and not the trust everything online and sometimes the information online is not true. AI detects inappropriate text using Natural Language Processing (NLP) and Machine Learning (ML) to analyze language patterns, conversational history, and user intent. Rather than just scanning for bad words, systems evaluate how words are used together to assess context, tone, and underlying meaning
- categories:
Hate Speech & Harassment: Slurs, identity-based attacks, terms related to supremacy, doxing threats, and derogatory generalizations.
Violence & Gore: Explicit descriptions of physical harm, torture, weapons manufacturing, and gore. Words like "kill," "stab," "shoot," or "bleed" are frequently evaluated for severity.
Self-Harm: Mentions of suicide, cutting, eating disorders, and phrases that express hopelessness or intent to cause self-injury.
Sexual Content (NSFW): Graphic anatomical terms, descriptions of sexual acts, and highly suggestive or racy slang.
Illegal Acts & Drugs: Terms related to purchasing or manufacturing illicit drugs, hacking, bomb-making, trafficking, or evading law enforcement. Use Gemini API key to detect 

We are young dumb broke highschool kids
We are doing a hackathon
We are not actually storing data because we have no users!