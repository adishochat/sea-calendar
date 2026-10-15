# הגדרת גישה ל-Google Calendar (חד-פעמי, בערך 10 דקות)

הסקריפט כותב ליומן שלך דרך ה-API של Google. לשם כך צריך "אפליקציה" משלך ב-Google Cloud. היא רק שלך, בחינם, ואף אחד אחר לא משתמש בה. שמות התפריטים ב-Google משתנים מדי פעם, ולכן ייתכן שמשהו ייראה מעט שונה.

## 1. פרויקט והפעלת ה-API

1. נכנסים ל-https://console.cloud.google.com עם חשבון ה-Google שבו נמצא היומן.
2. למעלה בוחרים **New project**, נותנים שם (למשל `sea-calendar`) ויוצרים.
3. בתפריט: **APIs & Services → Library**, מחפשים **Google Calendar API** ולוחצים **Enable**.

## 2. מסך ההסכמה (OAuth consent)

1. **APIs & Services → OAuth consent screen** (או **Google Auth Platform**).
2. סוג משתמשים: **External**. שם אפליקציה: `sea-calendar`. מייל תמיכה ומייל מפתח: המייל שלך.
3. אין צורך להוסיף scopes ידנית.
4. **חשוב מאוד:** במצב **Testing** ה-token שמתקבל פג אחרי 7 ימים והסקריפט יפסיק לעבוד. לכן לוחצים **Publish app** ואז **In production**. אפליקציה אישית שלא עברה אימות של Google עדיין עובדת, ובזמן ההתחברות תראה אזהרה (ראה למטה). ככל שאני יודע, זה מותר עד 100 משתמשים, ואתה המשתמש היחיד.

## 3. פרטי ההתחברות

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. סוג: **Desktop app**. שם: `sea-calendar`.
3. לוחצים **Download JSON**.
4. שומרים את הקובץ במחשב בשם הזה בדיוק, ובתיקייה הזאת:

```
~/.sea-calendar/client_secret.json
```

(התיקייה נוצרת בפקודה `python3 sea_calendar.py init`. ב-Finder: Go → Go to Folder → `~/.sea-calendar`.)

## 4. התחברות

```
python3 ~/.sea-calendar/sea_calendar.py auth
```

נפתח דפדפן. בוחרים את החשבון. תופיע אזהרה "Google hasn't verified this app": לוחצים **Advanced** ואז **Go to sea-calendar (unsafe)**. זו האפליקציה שיצרת בעצמך. מאשרים גישה ליומן. בסוף תראה בדפדפן "Signed in", ובטרמינל "signed in; token saved".

ה-token נשמר ב-`~/.sea-calendar/token.json` עם הרשאות קריאה רק לך. אם אי פעם תרצה לבטל את הגישה: https://myaccount.google.com/permissions

## אם משהו נתקע

- **"Google did not return a refresh token"**: מבטלים את הגישה הקיימת בקישור למעלה ומריצים `auth` שוב.
- **"Access blocked"**: כנראה שהאפליקציה עדיין ב-Testing וחשבון המייל שלך לא נוסף כ-Test user. הפתרון הנכון הוא לפרסם ל-In production (שלב 2).
- **"invalid_grant" אחרי שבוע**: האפליקציה נשארה ב-Testing. מפרסמים ומריצים `auth` שוב.
